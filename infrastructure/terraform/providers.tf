terraform {
  required_version = ">= 1.7.0"
  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.50"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "5GCityVerse"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}