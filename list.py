#!/usr/bin/env python3
"""Project タグごとにリソースを一覧し、コスト配分タグの状態を出す。

    ./list.py            全プロジェクト + タグ無し
    ./list.py gnos       指定したプロジェクトのみ

タグは sweeper-guardrail が削除を拒否する根拠。ここに出ないリソースは
守られていないので、タグの付け漏れを見つけるのが主目的。

Cost Explorer は 1リクエスト $0.01。--cost を付けたときだけ1回叩く。
"""

import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# [NOTE] terraform/main.tf の guardrail が見ているキーと同じもの
# [NOTE] ここを可変にすると、守られていないキーで一覧できてしまい誤解を生む
KEY = "Project"

# [NOTE] Tagging API はリージョン単位。global なものは個別に引く必要がある
GLOBAL = [
    ("cloudfront", ["cloudfront", "list-distributions",
                    "--query", "DistributionList.Items[].ARN"]),
    ("acm(us-east-1)", ["acm", "list-certificates", "--region", "us-east-1",
                        "--query", "CertificateSummaryList[].CertificateArn"]),
    ("iam role", ["iam", "list-roles", "--query", "Roles[].Arn"]),
    ("iam policy", ["iam", "list-policies", "--scope", "Local",
                    "--query", "Policies[].Arn"]),
]


def jq(*args):
    r = subprocess.run(["aws", *args, "--output", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout) or [] if r.stdout.strip() else []
    except json.JSONDecodeError:
        return []


def tags_of(arn):
    """global リソースはサービスごとに API が違う。"""
    svc = arn.split(":")[2]
    if svc == "cloudfront":
        d = jq("cloudfront", "list-tags-for-resource", "--resource", arn,
               "--query", "Tags.Items")
        return {t["Key"]: t["Value"] for t in d or []}
    if svc == "acm":
        d = jq("acm", "list-tags-for-certificate", "--region", "us-east-1",
               "--certificate-arn", arn, "--query", "Tags")
        return {t["Key"]: t["Value"] for t in d or []}
    if svc == "iam":
        kind, name = arn.split(":")[5].split("/")[0], arn.split("/")[-1]
        if kind == "role":
            d = jq("iam", "list-role-tags", "--role-name", name, "--query", "Tags")
        else:
            d = jq("iam", "list-policy-tags", "--policy-arn", arn, "--query", "Tags")
        return {t["Key"]: t["Value"] for t in d or []}
    return {}


def collect():
    """{project: [arn, ...]} と、タグ無しの ARN 一覧を返す。"""
    tagged, untagged = defaultdict(list), []

    # リージョン内は Tagging API が一括で返す
    for r in jq("resourcegroupstaggingapi", "get-resources",
                "--query", "ResourceTagMappingList") or []:
        t = {x["Key"]: x["Value"] for x in r["Tags"]}
        (tagged[t[KEY]].append(r["ResourceARN"]) if KEY in t
         else untagged.append(r["ResourceARN"]))

    seen = {a for v in tagged.values() for a in v} | set(untagged)

    # [NOTE] Tagging API に出ない global リソースは1件ずつ引くしかない。
    # [NOTE] aws CLI は1回の起動に約1.3秒かかるので、並列で回さないと数十秒になる
    targets = []
    for _, args in GLOBAL:
        for arn in jq(*args) or []:
            # AWS のサービスリンクロールはタグを持てず、削除対象でもない
            if arn in seen or ":role/aws-service-role/" in arn:
                continue
            seen.add(arn)
            targets.append(arn)

    with ThreadPoolExecutor(max_workers=16) as pool:
        for arn, t in zip(targets, pool.map(tags_of, targets)):
            (tagged[t[KEY]].append(arn) if KEY in t else untagged.append(arn))
    return tagged, untagged


def short(arn):
    """arn:aws:<svc>:<region>:<acct>:<残り> の 6番目以降が名前。
    lambda や logs は : でさらに区切るので、6要素で切って繋ぎ直す。"""
    p = arn.split(":", 5)
    svc, rest = p[2], (p[5] if len(p) > 5 else arn)
    return f"{svc:11} {rest}"


def cost_status():
    d = jq("ce", "list-cost-allocation-tags", "--query",
           f"CostAllocationTags[?TagKey=='{KEY}']")
    if not d:
        return "未取込"
    return d[0]["Status"]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    tagged, untagged = collect()

    for proj in sorted(tagged):
        if only and proj != only:
            continue
        print(f"\n=== {KEY}={proj}  ({len(tagged[proj])}) ===")
        for arn in sorted(tagged[proj]):
            print("  " + short(arn))

    if only:
        return

    print(f"\n=== タグ無し  ({len(untagged)}) ===")
    for arn in sorted(untagged):
        print("  " + short(arn))

    print(f"\n=== コスト配分タグ {KEY}: {cost_status()} ===")


if __name__ == "__main__":
    main()
