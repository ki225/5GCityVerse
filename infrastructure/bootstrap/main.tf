resource "aws_s3_bucket" "terraform_state" {
  bucket = var.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name       = var.state_bucket_name
    DataClass  = "terraform-state"
    ManagedFor = "5gcityverse-main-root"
  }
}

resource "aws_kms_key" "terraform_state" {
  description             = "Customer-managed key for ${var.state_bucket_name} Terraform state"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name       = "${var.state_bucket_name}-kms"
    DataClass  = "terraform-state"
    ManagedFor = "5gcityverse-main-root"
  }
}

resource "aws_kms_alias" "terraform_state" {
  name          = "alias/5gcityverse-tfstate-${substr(sha256(var.state_bucket_name), 0, 12)}"
  target_key_id = aws_kms_key.terraform_state.key_id
}

resource "aws_s3_bucket_ownership_controls" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.terraform_state.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

data "aws_iam_policy_document" "terraform_state" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.terraform_state.arn,
      "${aws_s3_bucket.terraform_state.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  policy = data.aws_iam_policy_document.terraform_state.json

  depends_on = [aws_s3_bucket_public_access_block.terraform_state]
}

output "state_bucket_name" {
  description = "S3 bucket name consumed by the main root backend configuration"
  value       = aws_s3_bucket.terraform_state.id
}

output "state_kms_key_arn" {
  description = "KMS key ARN consumed by the main S3 backend configuration"
  value       = aws_kms_key.terraform_state.arn
}

output "state_kms_alias_name" {
  description = "Deterministic KMS alias used by bootstrap state recovery"
  value       = aws_kms_alias.terraform_state.name
}
