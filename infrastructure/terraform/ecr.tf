resource "aws_ecr_repository" "smf_qer_actuator" {
  name                 = "${local.name_prefix}-smf-qer-actuator"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.frontend.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${local.name_prefix}-smf-qer-actuator"
  }
}

resource "aws_ecr_lifecycle_policy" "smf_qer_actuator" {
  repository = aws_ecr_repository.smf_qer_actuator.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove untagged build artifacts after seven days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecr_repository" "ueransim" {
  name                 = "${local.name_prefix}-ueransim"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.frontend.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${local.name_prefix}-ueransim"
  }
}

resource "aws_ecr_lifecycle_policy" "ueransim" {
  repository = aws_ecr_repository.ueransim.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove untagged build artifacts after seven days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
