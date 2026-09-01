# aws-ops

Delete leftover AWS resources.

```bash
./sweep.py            # list only
./sweep.py --delete
```

Always runs as the `sweeper` profile, whatever the caller's environment says.

```bash
./cost.py [months]
```

Splits spend into Usage and Credit — a credited account shows `$0.00` while still
burning budget. Also prints free tier headroom; a quota absent from the list has
none at all. Costs $0.02 per run; Cost Explorer bills per API call.

## Protection

The script holds no list of what to spare. It tries to delete everything, and the
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
