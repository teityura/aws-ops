#!/usr/bin/env python3
"""コストを Usage / Credit に分けて表示し、無料枠の残量も出す。

    ./cost.py [月数]     既定6ヶ月

Cost Explorer API は 1リクエスト $0.01 かかる。この実行で3回叩く。
"""

import json
import subprocess
import sys
from datetime import date

MONTHS = int(sys.argv[1]) if len(sys.argv) > 1 else 6

# [NOTE] guardrail と list.py が見ているキーと同じもの
TAG_KEY = "Project"


def jq(*args):
    r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr.strip().splitlines()[-1] if r.stderr else "error")
    return json.loads(r.stdout)


def bar(value, top, width=32):
    return "█" * max(1, round(value / top * width)) if value > 0 else ""


def period():
    end = date.today().replace(day=1)
    y, m = end.year, end.month - MONTHS
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}-01", date.today().isoformat()


def ce(group_key, extra_filter=None, kind="DIMENSION"):
    start, end = period()
    args = ["ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
            "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
            "--group-by", f"Type={kind},Key={group_key}"]
    if extra_filter:
        args += ["--filter", json.dumps(extra_filter)]
    return jq(*args)["ResultsByTime"]


def last_with_data(results):
    """[-1] は当月を掴む。月初だと空なので、Groups がある最後の月を返す。"""
    for t in reversed(results):
        if t["Groups"]:
            return t
    return results[-1]


def main():
    print("=" * 62)
    print("月別  Usage（実使用） / Credit（相殺） / 実支払")
    print("=" * 62)
    rows = []
    for t in ce("RECORD_TYPE"):
        d = {g["Keys"][0]: float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in t["Groups"]}
        usage, credit = d.get("Usage", 0), d.get("Credit", 0)
        rows.append((t["TimePeriod"]["Start"][:7], usage, credit, usage + credit))
    top = max((r[1] for r in rows), default=1) or 1
    for ym, usage, credit, net in rows:
        print(f"  {ym}  使用 {usage:9.4f}  相殺 {credit:9.4f}  支払 {net:8.4f}  {bar(usage, top)}")

    print()
    print("=" * 62)
    print("直近月  サービス別の実使用（クレジット控除前）")
    print("=" * 62)
    svc = last_with_data(ce("SERVICE", {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}))
    items = sorted(((float(g["Metrics"]["UnblendedCost"]["Amount"]), g["Keys"][0])
                    for g in svc["Groups"]), reverse=True)
    items = [(a, s) for a, s in items if a > 0.00001]
    top = items[0][0] if items else 1
    for amt, name in items:
        print(f"  {amt:9.4f} USD  {name[:34]:34} {bar(amt, top, 20)}")
    print(f"  {sum(a for a, _ in items):9.4f} USD  合計")

    print()
    print("=" * 62)
    proj = last_with_data(ce(TAG_KEY, {"Dimensions": {"Key": "RECORD_TYPE",
                             "Values": ["Usage"]}}, kind="TAG"))
    proj_month = proj["TimePeriod"]["Start"][:7]
    print(f"{proj_month}  {TAG_KEY} タグの値ごとの実使用")
    print("=" * 62)
    # [NOTE] コスト配分タグを有効化するまで、全額が値なし（Project$）に寄る
    # [NOTE] 有効化しても遡及しないので、それ以前の月は値なしのまま
    rows = sorted(((float(g["Metrics"]["UnblendedCost"]["Amount"]),
                    g["Keys"][0].split("$", 1)[1] or "（値なし）")
                   for g in proj["Groups"]), reverse=True)
    # [NOTE] サービス別と違い閾値で切らない。0 に近くても値の内訳を見たいため
    if rows:
        top = rows[0][0] or 1
        for amt, name in rows:
            print(f"  {amt:12.7f} USD  {name[:31]:31} {bar(amt, top, 20)}")
    else:
        print("  （データなし）")
    if len(rows) == 1 and rows[0][1] == "（値なし）":
        print(f"  ※ 全額が値なし。コスト配分タグ {TAG_KEY} が未有効の可能性がある")
        print(f"     aws ce update-cost-allocation-tags-status "
              f"--cost-allocation-tags-status TagKey={TAG_KEY},Status=Active")

    print()
    print("=" * 62)
    print("無料枠の消費（Always Free / 12ヶ月無料）")
    print("=" * 62)
    for u in jq("freetier", "get-free-tier-usage", "--region", "us-east-1")["freeTierUsages"]:
        limit = u.get("limit") or 0
        used = u.get("actualUsageAmount") or 0
        pct = used / limit * 100 if limit else 0
        print(f"  {u['service'][:20]:20} {u['usageType'][:22]:22} "
              f"{pct:5.1f}%  {used:.4g}/{limit:.6g} {u.get('unit','')[:12]}")
    print("\n  ※ 項目に無いものは無料枠が存在しない（DynamoDB のオンデマンド読み書きなど）")


if __name__ == "__main__":
    main()
