variable "name" {
  description = "掃除用IAMユーザ名。sweep.py の SWEEP_PRINCIPAL と一致させる"
  type        = string
}

variable "aws_region" {
  description = "AWSリージョン"
  type        = string
}
