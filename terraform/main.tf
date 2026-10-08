terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

resource "aws_iam_user" "sweeper" {
  name = var.name
}

# iam:CreateUser / CreatePolicyVersion / Detach*Policy を含めないことで
# sweeper が自分の制約を外せないようにしている
locals {
  delete_actions = [
    "acm:DeleteCertificate",
    "apigateway:DELETE",
    "cloudformation:DeleteStack",
    "cloudfront:DeleteDistribution",
    "cloudfront:UpdateDistribution",
    "codebuild:DeleteProject",
    "codeconnections:DeleteConnection",
    "codepipeline:DeletePipeline",
    "dynamodb:DeleteTable",
    "ec2:DeleteInternetGateway",
    "ec2:DeleteNatGateway",
    "ec2:DeleteRouteTable",
    "ec2:DeleteSecurityGroup",
    "ec2:DeleteSubnet",
    "ec2:DeleteVpc",
    "ec2:DetachInternetGateway",
    "ec2:ReleaseAddress",
    "ec2:TerminateInstances",
    "ecr:DeleteRepository",
    "ecs:DeleteCluster",
    "ecs:DeleteService",
    "elasticloadbalancing:DeleteLoadBalancer",
    "elasticloadbalancing:DeleteTargetGroup",
    "events:DeleteRule",
    "events:RemoveTargets",
    "glue:DeleteSession",
    "iam:DeleteInstanceProfile",
    "iam:DeletePolicy",
    "iam:DeletePolicyVersion",
    "iam:DeleteRole",
    "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy",
    "iam:RemoveRoleFromInstanceProfile",
    "lambda:DeleteFunction",
    "lambda:DeleteFunctionUrlConfig",
    "lambda:RemovePermission",
    "logs:DeleteLogGroup",
    "route53:ChangeResourceRecordSets",
    "route53:DeleteHostedZone",
    "s3:DeleteBucket",
    "s3:DeleteBucketWebsite",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "sns:DeleteTopic",
    "sqs:DeleteQueue",
  ]
}

resource "aws_iam_policy" "delete" {
  name        = "${var.name}-delete"
  description = "削除アクションのみ。IAMユーザ操作とポリシー改変は含まない"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DeleteOnly"
      Effect   = "Allow"
      Action   = local.delete_actions
      Resource = "*"
    }]
  })
}

resource "aws_iam_user_policy_attachment" "delete" {
  user       = aws_iam_user.sweeper.name
  policy_arn = aws_iam_policy.delete.arn
}

resource "aws_iam_user_policy_attachment" "readonly" {
  user       = aws_iam_user.sweeper.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# [NOTE] S3 は ABAC を有効にしたバケットだけ、タグの条件を評価する
# [NOTE] 各プロジェクトは aws_s3_bucket_abac を Enabled にする必要がある
# [NOTE] ABAC が無効のバケットには、この Deny がバケットにもオブジェクトにも効かない
resource "aws_iam_policy" "guardrail" {
  name        = "${var.name}-guardrail"
  description = "Project タグ付きリソースの削除を拒否。タグの値は見ない"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DenyTagged"
        Effect   = "Deny"
        Action   = local.delete_actions
        Resource = "*"
        Condition = {
          Null = { "aws:ResourceTag/Project" = "false" }
        }
      },
      {
        # IAM だけ条件キーの名前が違う
        Sid      = "DenyTaggedIam"
        Effect   = "Deny"
        Action   = [for a in local.delete_actions : a if startswith(a, "iam:")]
        Resource = "*"
        Condition = {
          Null = { "iam:ResourceTag/Project" = "false" }
        }
      },
    ]
  })
}

resource "aws_iam_user_policy_attachment" "guardrail" {
  user       = aws_iam_user.sweeper.name
  policy_arn = aws_iam_policy.guardrail.arn
}

# アクセスキーは state に平文で入るため Terraform で管理しない
#   aws iam create-access-key --user-name sweeper
