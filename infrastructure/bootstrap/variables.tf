variable "aws_region" {
  description = "AWS region for the state bucket"
  type        = string
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "prod"
}

variable "state_bucket_name" {
  description = "Globally unique S3 bucket name used by the main Terraform root"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name."
  }
}
