# aws-ops

Delete leftover AWS resources, and see what is protected and what it costs.

```bash
./sweep.py                 # list only
./sweep.py --delete
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
```

Untagged means unprotected.

```bash
./cost.py                  # last 6 months
./cost.py 12
```

```
2026-08  Project タグの値ごとの実使用
    86.8490639 USD  （値なし）                        ████████████████████
  ※ コスト配分タグ Project が未有効
```

Tag values reach the billing data only after activation — up to a day, never
retroactive. Cost Explorer bills per API call; `cost.py` makes three.

```bash
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=Project,Status=Active
```

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

`aws iam simulate-principal-policy` populates neither `aws:PrincipalArn` nor
resource tags. Pass them via `--context-entries`, or negated conditions such as
`ArnNotLike` and the tag guardrail both evaluate wrong — a protected bucket comes
back `allowed`. Verify protection by attempting the delete, not by simulating.
