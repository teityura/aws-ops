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
    "apigateway:DELETE",
    "codebuild:DeleteProject",
    "codepipeline:DeletePipeline",
    "dynamodb:DeleteTable",
    "ec2:DeleteNatGateway",
    "ec2:DeleteSecurityGroup",
    "ec2:DeleteSubnet",
    "ec2:DeleteVpc",
    "ec2:ReleaseAddress",
    "ec2:TerminateInstances",
    "ecr:DeleteRepository",
    "ecs:DeleteCluster",
    "ecs:DeleteService",
    "events:DeleteRule",
    "events:RemoveTargets",
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
    "s3:DeleteBucketPolicy",
    "s3:DeleteBucketWebsite",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "sns:DeleteTopic",
    "sqs:DeleteQueue",
  ]
}

resource "aws_iam_policy" "sweeper_delete" {
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
  policy_arn = aws_iam_policy.sweeper_delete.arn
}

resource "aws_iam_user_policy_attachment" "readonly" {
  user       = aws_iam_user.sweeper.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# アクセスキーは state に平文で入るため Terraform で管理しない
#   aws iam create-access-key --user-name sweeper
