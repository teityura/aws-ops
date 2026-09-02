# aws-ops

Delete leftover AWS resources, and see what is protected and what it costs.

```bash
./sweep.py                 # list only
./sweep.py --delete        # delete, after a confirmation
```

```bash
./list.py                  # every Project tag value, plus untagged
./list.py gnos
```

```
=== Project=gnos  (9) ===
  dynamodb    table/gnos-games
  events      rule/gnos-schedule
  iam         role/gnos-lambda-role
  lambda      function:gnos-api
  logs        log-group:/aws/lambda/gnos-api
  s3          gnos-site

=== コスト配分タグ Project: 未取込 ===
```

Untagged means unprotected — a leftover to remove, or a tag someone forgot.
AWS service-linked roles are skipped; they cannot be tagged or deleted.

```bash
./cost.py                  # last 6 months
./cost.py 12
```

```
2026-08  サービス別の実使用（クレジット控除前）
    30.3398 USD  EC2 - Other                        ████████████████████
    29.9813 USD  Amazon Elastic Container Service   ████████████████████
    86.8491 USD  合計

2026-08  Project タグの値ごとの実使用
    86.8490639 USD  （値なし）                        ████████████████████
```

Values stay empty until the tag is activated for cost allocation — up to a day
after it first appears, and never retroactive:

```bash
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=Project,Status=Active
```

Cost Explorer bills per API call; `cost.py` makes three.

## Protection

`sweep.py` holds no list of what to spare. It tries to delete everything, and the
survivors are whatever AWS refuses.

| | |
|---|---|
| `sweeper-guardrail` | denies deleting anything tagged `Project`, whatever the value |
| project bucket policies | S3 has no tag condition, so each bucket refuses deletes itself |
| `sweeper` permissions | allowlist of delete actions |
| `exclude.toml` | exact ARN match. Last resort |

To spare something, tag it — never change the script.

## Setup

```bash
cp exclude.toml.sample exclude.toml

cd terraform
cp terraform.tfvars.sample terraform.tfvars
terraform init && terraform apply     # as an admin

aws iam create-access-key --user-name sweeper
```

## Gotchas

Named profiles need `region` in `~/.aws/config`. Without it every regional service
silently drops out of the listing, while IAM, S3 and Route53 still work.

`aws iam simulate-principal-policy` does not populate `aws:PrincipalArn`. Pass it via
`--context-entries`, or negated conditions such as `ArnNotLike` evaluate wrong.
