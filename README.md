# aws-ops

Delete leftover AWS resources.

```bash
./sweep.py            # list only
./sweep.py --delete
```

It always runs as the `sweeper` profile — the calling environment cannot change that.

## Design

The script holds no rules about what to keep. It tries to delete everything it lists.
Protection lives outside it:

| | where | |
|---|---|---|
| 1 | each project's guardrail | AWS denies it. Scales as projects are added |
| 2 | `sweeper` permissions | allowlist of delete actions only |
| 3 | `exclude.toml` | exact ARN match. Last resort |

To keep something, change 2 or 3 — never the script.

`sweeper` has no `iam:CreateUser`, `iam:Detach*Policy` or `iam:CreatePolicyVersion`,
so it cannot lift its own restrictions. With an allowlist a missing entry means
"does not work", not "silently permitted".

Running as an admin would bypass the guardrails, which exempt the Terraform principal,
so the profile is fixed in the script rather than left to the caller.

## Setup

```bash
cp exclude.toml.sample exclude.toml

cd terraform
cp terraform.tfvars.sample terraform.tfvars
terraform init && terraform apply     # run as an admin

aws iam create-access-key --user-name sweeper
```

Access keys are not managed by Terraform — they would land in the state file.

Each project's guardrail should attach to every IAM user (`data "aws_iam_users"`),
so a new project protects `sweeper` automatically after one `terraform apply`.

## Gotchas

Named profiles need `region` in `~/.aws/config`. Without it every regional service
silently drops out of the listing, while IAM, S3 and Route53 still work.

`aws iam simulate-principal-policy` does not populate `aws:PrincipalArn`. Pass it via
`--context-entries`, or negated conditions such as `ArnNotLike` evaluate wrong.
