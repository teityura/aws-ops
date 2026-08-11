#!/usr/bin/env python3
"""一覧に出たものを全部消しにいく。守る判断は guardrail と exclude.toml に外出しする。

    ./sweep.py            一覧のみ
    ./sweep.py --delete   確認の後に削除
"""

import json
import subprocess
import sys
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
        raise AwsError(r.stderr.strip().splitlines()[-1] if r.stderr else "error")
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

    inv["SNS トピック"] = [
        (a, a) for a in jq("sns", "list-topics", "--query", "Topics[].TopicArn")]

    inv["SQS キュー"] = [
        (f"arn:aws:sqs:{region}:{acct}:{u.rstrip('/').split('/')[-1]}", u)
        for u in jq("sqs", "list-queues", "--query", "QueueUrls")]

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
            aws("s3api", "delete-objects", "--bucket", k,
                "--delete", json.dumps({"Objects": objs, "Quiet": True}))
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


def del_sns(k):
    aws("sns", "delete-topic", "--topic-arn", k)


def del_sqs(k):
    aws("sqs", "delete-queue", "--queue-url", k)


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
    "SNS トピック": del_sns,
    "SQS キュー": del_sqs,
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

    ok, ng = [], []
    for svc, items in targets.items():
        fn = DELETERS.get(svc)
        if not fn:
            continue
        for arn, key in items:
            try:
                fn(key)
                ok.append((svc, arn))
                print(f"  削除  {svc:24} {arn}")
            except AwsError as e:
                ng.append((svc, arn, str(e)))
                print(f"  拒否  {svc:24} {arn}")

    print(f"\n削除 {len(ok)} 件 / 拒否 {len(ng)} 件")
    if ng:
        print("\n拒否されたもの（理由つき）")
        for svc, arn, why in ng:
            print(f"  {arn}\n      {why}")


if __name__ == "__main__":
    main()
