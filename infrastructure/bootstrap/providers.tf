terraform {
  required_version = "~> 1.15.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.50.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "5GCityVerse"
      Environment = var.environment
      Terraform   = "bootstrap"
    }
  }
}
