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

# [NOTE] Tagging API はリージョン単位。us-east-1 を足すと CloudFront と ACM も一括で取れる
# [NOTE] IAM だけは一括でタグを引く API が無く、1件ずつ問い合わせるしかない
REGIONS = [None, "us-east-1"]
IAM_LISTS = [
    ["iam", "list-roles", "--query", "Roles[].Arn"],
    ["iam", "list-policies", "--scope", "Local", "--query", "Policies[].Arn"],
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
    """IAM のみ。ロールとポリシーで API が違う。"""
    kind, name = arn.split(":")[5].split("/")[0], arn.split("/")[-1]
    if kind == "role":
        d = jq("iam", "list-role-tags", "--role-name", name, "--query", "Tags")
    else:
        d = jq("iam", "list-policy-tags", "--policy-arn", arn, "--query", "Tags")
    return {t["Key"]: t["Value"] for t in d or []}


def bulk(region):
    args = ["resourcegroupstaggingapi", "get-resources",
            "--query", "ResourceTagMappingList"]
    if region:
        args += ["--region", region]
    return jq(*args) or []


def collect():
    """{project: [arn, ...]} と、タグ無しの ARN 一覧を返す。"""
    tagged, untagged = defaultdict(list), []

    def put(arn, t):
        (tagged[t[KEY]].append(arn) if KEY in t else untagged.append(arn))

    with ThreadPoolExecutor(max_workers=8) as pool:
        regions = list(pool.map(bulk, REGIONS))
        iam = list(pool.map(lambda a: jq(*a) or [], IAM_LISTS))

    seen = set()
    for rows in regions:
        for r in rows:
            # AWS が持つ請求用オブジェクト。消せないので一覧に出さない
            if r["ResourceARN"] in seen or ":payments:" in r["ResourceARN"]:
                continue
            seen.add(r["ResourceARN"])
            put(r["ResourceARN"], {x["Key"]: x["Value"] for x in r["Tags"]})

    # AWS が作るサービスリンクロールはタグを持てず、削除もできない
    targets = [a for arns in iam for a in arns
               if a not in seen and ":role/aws-service-role/" not in a]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for arn, t in zip(targets, pool.map(tags_of, targets)):
            put(arn, t)
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
