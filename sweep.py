#!/usr/bin/env python3
"""一覧に出たものを全部消しにいく。守る判断は guardrail と exclude.toml に外出しする。

    ./sweep.py            一覧のみ
    ./sweep.py --delete   確認の後に削除
"""

import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

DELETE = "--delete" in sys.argv
EXCLUDE_FILE = Path(__file__).resolve().parent / "exclude.toml"

# guardrail は Terraform 実行者を除外しているため、管理者で流すと素通りする。
# 呼び出し側に委ねず、常に sweeper で叩く（terraform/ が作る）。
PROFILE = "sweeper"

FAILED_LISTS = []


class AwsError(Exception):
    pass


def aws(*args):
    """失敗は例外。ポリシーを外せなければロールも消せないので途中で止めてよい。"""
    r = subprocess.run(["aws", "--profile", PROFILE, *args], capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "error"
        raise AwsError(msg.removeprefix("aws: [ERROR]: "))
    return r.stdout.strip()


def jq(*args):
    """握り潰すと一覧が静かに不完全になるので、失敗は FAILED_LISTS に残す。"""
    r = subprocess.run(["aws", "--profile", PROFILE, *args, "--output", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr.strip().splitlines()[-1] if r.stderr else "error"
        FAILED_LISTS.append(f"{' '.join(args[:2])}: {msg}")
        return []
    try:
        # 0件のとき AWS CLI は null を返すので [] に寄せる
        return json.loads(r.stdout) or [] if r.stdout.strip() else []
    except json.JSONDecodeError:
        return []


def settle(*args):
    """直前に消した物の後始末（ENI の解放など）が終わるまで、依存エラーの間だけ待って繰り返す。"""
    for _ in range(12):
        try:
            return aws(*args)
        except AwsError as e:
            if not any(x in str(e) for x in ("DependencyViolation", "ResourceInUse")):
                raise
            time.sleep(10)
    return aws(*args)


def verdict(msg):
    """AWS の応答を「保護」「権限なし」「失敗」に分ける。理由を出すのは後ろの2つだけ。"""
    if "explicit deny" in msg:
        return "保護", ""
    m = re.search(r"not authorized to perform: (\S+)", msg)
    if m:
        return "権限なし", m.group(1)
    m = re.search(r"\((\w+)\) when calling the \w+ operation: (.*)", msg)
    return "失敗", (f"{m.group(1)}: {m.group(2)}" if m else msg)[:200]


def load_excludes():
    """書式違反は「除外されず削除される」を意味するので即座に止める。"""
    if not EXCLUDE_FILE.exists():
        return set()
    try:
        data = tomllib.loads(EXCLUDE_FILE.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{EXCLUDE_FILE}: {e}")
    out = set(data.get("exclude", []))
    bad = sorted(str(a) for a in out if not str(a).startswith("arn:"))
    if bad:
        sys.exit(f"{EXCLUDE_FILE}: ARN ではありません -> {', '.join(bad)}")
    return out


# 各要素は (arn, key)。arn は除外判定用、key は削除APIに渡す識別子。
def inventory(acct, region):
    inv = {}

    inv["Lambda 関数"] = [
        (f["Arn"], f["Name"]) for f in
        jq("lambda", "list-functions", "--query",
           "Functions[].{Arn:FunctionArn,Name:FunctionName}")]

    inv["DynamoDB テーブル"] = [
        (f"arn:aws:dynamodb:{region}:{acct}:table/{n}", n)
        for n in jq("dynamodb", "list-tables", "--query", "TableNames")]

    inv["S3 バケット"] = [
        (f"arn:aws:s3:::{n}", n)
        for n in jq("s3api", "list-buckets", "--query", "Buckets[].Name")]

    inv["CloudWatch ロググループ"] = [
        (f"arn:aws:logs:{region}:{acct}:log-group:{n}", n)
        for n in jq("logs", "describe-log-groups", "--query", "logGroups[].logGroupName")]

    inv["EventBridge ルール"] = [
        (r["Arn"], r["Name"]) for r in
        jq("events", "list-rules", "--query", "Rules[].{Arn:Arn,Name:Name}")]

    inv["IAM ロール"] = [
        (r["Arn"], r["Name"]) for r in
        jq("iam", "list-roles", "--query",
           "Roles[?!starts_with(Path,`/aws-service-role/`)].{Arn:Arn,Name:RoleName}")]

    inv["IAM カスタムポリシー"] = [
        (p["Arn"], p["Arn"]) for p in
        jq("iam", "list-policies", "--scope", "Local", "--query",
           "Policies[].{Arn:Arn}")]

    inv["API Gateway (HTTP)"] = [
        (f"arn:aws:apigateway:{region}::/apis/{i}", i)
        for i in jq("apigatewayv2", "get-apis", "--query", "Items[].ApiId")]

    inv["API Gateway (REST)"] = [
        (f"arn:aws:apigateway:{region}::/restapis/{i}", i)
        for i in jq("apigateway", "get-rest-apis", "--query", "items[].id")]

    inv["Route53 ホストゾーン"] = [
        (f"arn:aws:route53:::hostedzone/{z['Id'].split('/')[-1]}", z["Id"].split("/")[-1])
        for z in jq("route53", "list-hosted-zones", "--query", "HostedZones[].{Id:Id}")]

    inv["ECR リポジトリ"] = [
        (r["Arn"], r["Name"]) for r in
        jq("ecr", "describe-repositories", "--query",
           "repositories[].{Arn:repositoryArn,Name:repositoryName}")]

    inv["CodePipeline パイプライン"] = [
        (f"arn:aws:codepipeline:{region}:{acct}:{n}", n)
        for n in jq("codepipeline", "list-pipelines", "--query", "pipelines[].name")]

    inv["CodeBuild プロジェクト"] = [
        (f"arn:aws:codebuild:{region}:{acct}:project/{n}", n)
        for n in jq("codebuild", "list-projects", "--query", "projects")]

    inv["CodeConnections 接続"] = [
        (a, a) for a in
        jq("codeconnections", "list-connections", "--query", "Connections[].ConnectionArn")]

    inv["SNS トピック"] = [
        (a, a) for a in jq("sns", "list-topics", "--query", "Topics[].TopicArn")]

    inv["ACM 証明書"] = [
        (c, c)
        for reg in dict.fromkeys([region, "us-east-1"])
        for c in jq("acm", "list-certificates", "--region", reg,
                    "--query", "CertificateSummaryList[].CertificateArn")]

    inv["CloudFront ディストリビューション"] = [
        (d["Arn"], d["Id"]) for d in
        jq("cloudfront", "list-distributions", "--query",
           "DistributionList.Items[].{Arn:ARN,Id:Id}")]

    inv["SQS キュー"] = [
        (f"arn:aws:sqs:{region}:{acct}:{u.rstrip('/').split('/')[-1]}", u)
        for u in jq("sqs", "list-queues", "--query", "QueueUrls")]

    # [NOTE] ここから下は並び順が削除順。上を消し切らないと下が消せない
    clusters = jq("ecs", "list-clusters", "--query", "clusterArns")

    inv["ECS サービス"] = [
        (s, (c, s)) for c in clusters
        for s in jq("ecs", "list-services", "--cluster", c, "--query", "serviceArns")]

    inv["ECS クラスタ"] = [(c, c) for c in clusters]

    inv["EC2 インスタンス"] = [
        (f"arn:aws:ec2:{region}:{acct}:instance/{i}", i) for i in
        jq("ec2", "describe-instances", "--filters",
           "Name=instance-state-name,Values=pending,running,stopping,stopped",
           "--query", "Reservations[].Instances[].InstanceId")]

    inv["ロードバランサー"] = [
        (a, a) for a in
        jq("elbv2", "describe-load-balancers", "--query", "LoadBalancers[].LoadBalancerArn")]

    inv["ターゲットグループ"] = [
        (a, a) for a in
        jq("elbv2", "describe-target-groups", "--query", "TargetGroups[].TargetGroupArn")]

    inv["NAT Gateway"] = [
        (f"arn:aws:ec2:{region}:{acct}:natgateway/{i}", i) for i in
        jq("ec2", "describe-nat-gateways", "--filter", "Name=state,Values=pending,available",
           "--query", "NatGateways[].NatGatewayId")]

    # [NOTE] ALB が持つアドレスは ServiceManaged が付き、ALB と一緒に消えるので載せない
    inv["Elastic IP"] = [
        (f"arn:aws:ec2:{region}:{acct}:elastic-ip/{i}", i) for i in
        jq("ec2", "describe-addresses", "--query", "Addresses[?!ServiceManaged].AllocationId")]

    # [NOTE] デフォルト VPC とその付属物は AWS が用意した土台なので載せない
    # [NOTE] VPC の一覧に失敗するとデフォルトを見分けられないので、VPC まわりは丸ごと載せない
    failed = len(FAILED_LISTS)
    vpcs = jq("ec2", "describe-vpcs", "--query", "Vpcs[].{Id:VpcId,Default:IsDefault}")
    if len(FAILED_LISTS) == failed:
        default_vpcs = {v["Id"] for v in vpcs if v["Default"]}

        def ec2(kind, i):
            return f"arn:aws:ec2:{region}:{acct}:{kind}/{i}"

        # [NOTE] 他のグループから参照されているグループは消せないので、参照している側を先に並べる
        groups = jq("ec2", "describe-security-groups", "--query",
                    "SecurityGroups[?GroupName!=`default`]"
                    ".{Id:GroupId,Refs:length(IpPermissions[].UserIdGroupPairs[])}")
        inv["セキュリティグループ"] = [
            (ec2("security-group", g["Id"]), g["Id"])
            for g in sorted(groups, key=lambda g: -g["Refs"])]

        inv["サブネット"] = [
            (ec2("subnet", i), i) for i in
            jq("ec2", "describe-subnets", "--query", "Subnets[?!DefaultForAz].SubnetId")]

        inv["インターネットゲートウェイ"] = [
            (ec2("internet-gateway", g["Id"]), (g["Id"], g["Vpc"])) for g in
            jq("ec2", "describe-internet-gateways", "--query",
               "InternetGateways[].{Id:InternetGatewayId,Vpc:Attachments[0].VpcId}")
            if g["Vpc"] not in default_vpcs]

        # [NOTE] メインのルートテーブルは VPC と一緒に消えるので載せない
        inv["ルートテーブル"] = [
            (ec2("route-table", t["Id"]), t["Id"]) for t in
            jq("ec2", "describe-route-tables", "--query",
               "RouteTables[?!(Associations[?Main])].{Id:RouteTableId,Vpc:VpcId}")
            if t["Vpc"] not in default_vpcs]

        inv["VPC"] = [(ec2("vpc", v["Id"]), v["Id"]) for v in vpcs if not v["Default"]]

    # [NOTE] 中身を消し終えてから消す。先に消すと、手で足した物に阻まれて DELETE_FAILED で残る
    inv["CloudFormation スタック"] = [
        (s["Id"], s["Name"]) for s in
        jq("cloudformation", "describe-stacks", "--query",
           "Stacks[].{Id:StackId,Name:StackName}")]

    inv["Glue セッション"] = [
        (f"arn:aws:glue:{region}:{acct}:session/{i}", i)
        for i in jq("glue", "list-sessions", "--query", "Ids")]

    return {k: v for k, v in inv.items() if v}



def del_lambda(k):
    aws("lambda", "delete-function", "--function-name", k)


def del_dynamodb(k):
    aws("dynamodb", "delete-table", "--table-name", k)


def del_s3(k):
    for field in ("Versions", "DeleteMarkers"):  # 空にしないとバケットを消せない
        while True:
            objs = jq("s3api", "list-object-versions", "--bucket", k,
                      "--max-items", "1000", "--query",
                      f"{field}[].{{Key:Key,VersionId:VersionId}}")
            if not objs:
                break
            # delete-objects はキー単位で拒否されても終了コード0を返すため、
            # 応答の Errors を見ないと消えないまま無限ループする
            out = aws("s3api", "delete-objects", "--bucket", k, "--output", "json",
                      "--delete", json.dumps({"Objects": objs, "Quiet": True}))
            errs = (json.loads(out) if out else {}).get("Errors") or []
            if errs:
                raise AwsError(f"{errs[0].get('Code')}: {errs[0].get('Message')}")
    aws("s3api", "delete-bucket", "--bucket", k)


def del_logs(k):
    aws("logs", "delete-log-group", "--log-group-name", k)


def del_events(k):
    for t in jq("events", "list-targets-by-rule", "--rule", k, "--query", "Targets[].Id"):
        aws("events", "remove-targets", "--rule", k, "--ids", t)
    aws("events", "delete-rule", "--name", k)


def del_iam_role(k):
    for p in jq("iam", "list-attached-role-policies", "--role-name", k,
                "--query", "AttachedPolicies[].PolicyArn"):
        aws("iam", "detach-role-policy", "--role-name", k, "--policy-arn", p)
    for p in jq("iam", "list-role-policies", "--role-name", k, "--query", "PolicyNames"):
        aws("iam", "delete-role-policy", "--role-name", k, "--policy-name", p)
    for ip in jq("iam", "list-instance-profiles-for-role", "--role-name", k,
                 "--query", "InstanceProfiles[].InstanceProfileName"):
        aws("iam", "remove-role-from-instance-profile",
            "--instance-profile-name", ip, "--role-name", k)
    aws("iam", "delete-role", "--role-name", k)


def del_iam_policy(arn):
    ent = jq("iam", "list-entities-for-policy", "--policy-arn", arn)
    for u in ent.get("PolicyUsers", []):
        aws("iam", "detach-user-policy", "--user-name", u["UserName"], "--policy-arn", arn)
    for r in ent.get("PolicyRoles", []):
        aws("iam", "detach-role-policy", "--role-name", r["RoleName"], "--policy-arn", arn)
    for g in ent.get("PolicyGroups", []):
        aws("iam", "detach-group-policy", "--group-name", g["GroupName"], "--policy-arn", arn)
    for v in jq("iam", "list-policy-versions", "--policy-arn", arn,
                "--query", "Versions[?!IsDefaultVersion].VersionId"):
        aws("iam", "delete-policy-version", "--policy-arn", arn, "--version-id", v)
    aws("iam", "delete-policy", "--policy-arn", arn)


def del_apigw_http(k):
    aws("apigatewayv2", "delete-api", "--api-id", k)


def del_apigw_rest(k):
    aws("apigateway", "delete-rest-api", "--rest-api-id", k)


def del_route53(zid):
    rrs = jq("route53", "list-resource-record-sets", "--hosted-zone-id", zid,
             "--query", "ResourceRecordSets[?Type!='NS' && Type!='SOA']")
    if rrs:
        aws("route53", "change-resource-record-sets", "--hosted-zone-id", zid,
            "--change-batch",
            json.dumps({"Changes": [{"Action": "DELETE", "ResourceRecordSet": r}
                                    for r in rrs]}))
    aws("route53", "delete-hosted-zone", "--id", zid)


def del_ecr(k):
    aws("ecr", "delete-repository", "--repository-name", k, "--force")


def del_codepipeline(k):
    aws("codepipeline", "delete-pipeline", "--name", k)


def del_codebuild(k):
    aws("codebuild", "delete-project", "--name", k)


def del_codeconnections(arn):
    aws("codeconnections", "delete-connection", "--connection-arn", arn)


def del_sns(k):
    aws("sns", "delete-topic", "--topic-arn", k)


def del_acm(arn):
    aws("acm", "delete-certificate", "--region", arn.split(":")[3], "--certificate-arn", arn)


def del_cloudfront(k):
    # 無効化して伝播を待つ必要があり、この場では消せない（拒否として表示される）
    aws("cloudfront", "delete-distribution", "--id", k)


def del_sqs(k):
    aws("sqs", "delete-queue", "--queue-url", k)


def del_ecs_service(k):
    cluster, svc = k
    aws("ecs", "delete-service", "--cluster", cluster, "--service", svc, "--force")
    # 消え切る前はクラスタを消せない
    aws("ecs", "wait", "services-inactive", "--cluster", cluster, "--services", svc)


def del_ecs_cluster(k):
    aws("ecs", "delete-cluster", "--cluster", k)


def del_ec2_instance(k):
    aws("ec2", "terminate-instances", "--instance-ids", k)


def del_elb(k):
    aws("elbv2", "delete-load-balancer", "--load-balancer-arn", k)
    # 消え切る前はターゲットグループを消せない
    aws("elbv2", "wait", "load-balancers-deleted", "--load-balancer-arns", k)


def del_target_group(k):
    settle("elbv2", "delete-target-group", "--target-group-arn", k)


def del_nat(k):
    aws("ec2", "delete-nat-gateway", "--nat-gateway-id", k)
    # 消え切る前は EIP を解放できない
    aws("ec2", "wait", "nat-gateway-deleted", "--nat-gateway-ids", k)


def del_eip(k):
    aws("ec2", "release-address", "--allocation-id", k)


def del_security_group(k):
    settle("ec2", "delete-security-group", "--group-id", k)


def del_subnet(k):
    settle("ec2", "delete-subnet", "--subnet-id", k)


def del_igw(k):
    igw, vpc = k
    if vpc:
        settle("ec2", "detach-internet-gateway", "--internet-gateway-id", igw, "--vpc-id", vpc)
    aws("ec2", "delete-internet-gateway", "--internet-gateway-id", igw)


def del_route_table(k):
    settle("ec2", "delete-route-table", "--route-table-id", k)


def del_vpc(k):
    settle("ec2", "delete-vpc", "--vpc-id", k)


def del_stack(k):
    aws("cloudformation", "delete-stack", "--stack-name", k)
    try:
        aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", k)
    except AwsError:
        # 中身は先に消してある。CloudFormation が後始末に使う権限まで sweeper に持たせず、記録だけ消す
        aws("cloudformation", "delete-stack", "--stack-name", k,
            "--deletion-mode", "FORCE_DELETE_STACK")
        aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", k)


def del_glue_session(k):
    aws("glue", "delete-session", "--id", k)


DELETERS = {
    "Lambda 関数": del_lambda,
    "DynamoDB テーブル": del_dynamodb,
    "S3 バケット": del_s3,
    "CloudWatch ロググループ": del_logs,
    "EventBridge ルール": del_events,
    "IAM ロール": del_iam_role,
    "IAM カスタムポリシー": del_iam_policy,
    "API Gateway (HTTP)": del_apigw_http,
    "API Gateway (REST)": del_apigw_rest,
    "Route53 ホストゾーン": del_route53,
    "ECR リポジトリ": del_ecr,
    "CodePipeline パイプライン": del_codepipeline,
    "CodeBuild プロジェクト": del_codebuild,
    "CodeConnections 接続": del_codeconnections,
    "SNS トピック": del_sns,
    "ACM 証明書": del_acm,
    "CloudFront ディストリビューション": del_cloudfront,
    "SQS キュー": del_sqs,
    "ECS サービス": del_ecs_service,
    "ECS クラスタ": del_ecs_cluster,
    "EC2 インスタンス": del_ec2_instance,
    "ロードバランサー": del_elb,
    "ターゲットグループ": del_target_group,
    "NAT Gateway": del_nat,
    "Elastic IP": del_eip,
    "セキュリティグループ": del_security_group,
    "サブネット": del_subnet,
    "インターネットゲートウェイ": del_igw,
    "ルートテーブル": del_route_table,
    "VPC": del_vpc,
    "CloudFormation スタック": del_stack,
    "Glue セッション": del_glue_session,
}



def main():
    who = aws("sts", "get-caller-identity", "--query", "Arn", "--output", "text")
    acct = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    region = aws("configure", "get", "region") or "ap-northeast-1"
    excludes = load_excludes()

    print(f"実行者: {who}")
    if EXCLUDE_FILE.exists():
        print(f"除外リスト: {len(excludes)} 件（{EXCLUDE_FILE.name}）\n")
    else:
        print(f"除外リスト: なし（{EXCLUDE_FILE.name} が存在しません）")
        print(f"  必要なら {EXCLUDE_FILE.name}.sample からコピーしてください\n")

    inv = inventory(acct, region)
    if FAILED_LISTS:
        print("一覧取得に失敗（見落としの可能性あり）")
        for f in FAILED_LISTS:
            print(f"  ! {f}")
        print()

    targets, skipped = {}, []
    for svc, items in inv.items():
        for arn, key in items:
            if arn in excludes:
                skipped.append((svc, arn))
            else:
                targets.setdefault(svc, []).append((arn, key))

    stale = excludes - {a for items in inv.values() for a, _ in items}
    if stale:
        print("除外リストに書かれているが該当リソースが無い（打ち間違い or 削除済み）")
        for a in sorted(stale):
            print(f"  ? {a}")
        print()

    for svc, arn in skipped:
        print(f"  除外  {svc:26} {arn}")
    for svc, items in targets.items():
        for arn, key in items:
            print(f"  対象  {svc:26} {arn}")

    total = sum(len(v) for v in targets.values())
    print(f"\n対象 {total} 件 / 除外 {len(skipped)} 件")

    if not total or not DELETE:
        if total and not DELETE:
            print("\n一覧のみ。削除するには --delete を付ける")
        return

    print("\n" + "!" * 64)
    print(f"アカウント {acct} / リージョン {region}")
    if input(f"{who} として {total} 件を削除します。続けるには DELETE と入力: ").strip() != "DELETE":
        print("中止しました")
        return

    done = {"削除": [], "保護": [], "権限なし": [], "失敗": []}
    try:
        for svc, items in targets.items():
            fn = DELETERS.get(svc)
            if not fn:
                continue
            for arn, key in items:
                try:
                    fn(key)
                    kind, why = "削除", ""
                except AwsError as e:
                    kind, why = verdict(str(e))
                except Exception as e:  # 1件の不具合で残りを止めない
                    kind, why = "失敗", f"{type(e).__name__}: {e}"
                done[kind].append((arn, why))
                # 全角は2桁ぶんの幅なので、桁数で詰める
                print(f"  {kind}{' ' * (10 - 2 * len(kind))}{svc:24} {arn}")
    except KeyboardInterrupt:
        print("\n中断しました。ここまでの結果")

    print("\n" + " / ".join(f"{k} {len(v)} 件" for k, v in done.items()))

    # [NOTE] 保護は狙いどおりの結果なので理由を並べない。見るべきは下の2つ
    if done["権限なし"]:
        print("\n権限なし（sweeper に許可していない操作）")
        for arn, action in done["権限なし"]:
            print(f"  {action:42} {arn}")
    if done["失敗"]:
        print("\n失敗")
        for arn, why in done["失敗"]:
            print(f"  {arn}\n      {why}")
        sys.exit(1)


if __name__ == "__main__":
    main()
