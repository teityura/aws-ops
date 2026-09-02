# aws-ops

Delete leftover AWS resources, and see what is protected and what it costs.

```bash
./sweep.py                 # list only
./sweep.py --delete        # delete, after a confirmation

./list.py                  # resources by Project tag value, plus untagged ones
./list.py gnos             # one project

./cost.py                  # spend, last 6 months
./cost.py 12               # last 12 months
```

`sweep.py` always runs as the `sweeper` profile, whatever the caller's
environment says.

`list.py` groups by the `Project` tag — the same key `sweeper-guardrail` denies
on. Anything under "untagged" is unprotected: either a leftover to remove, or a
tag someone forgot.

`cost.py` splits spend into Usage and Credit — a credited account shows `$0.00`
while still burning budget — then breaks the latest month down by service and by
`Project` tag value, and prints free tier headroom. A quota absent from that list
has none at all. Costs $0.03 per run; Cost Explorer bills per API call.

Tag values only reach the billing data once the tag is activated for cost
allocation, which takes up to a day after the tag first appears and never applies
retroactively:

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

`aws iam simulate-principal-policy` does not populate `aws:PrincipalArn`. Pass it via
`--context-entries`, or negated conditions such as `ArnNotLike` evaluate wrong.
