terraform {
  required_version = ">= 1.10.0" # S3-native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state backend. Terraform state for this stack contains
  # infrastructure identifiers and ARNs (not PHI, and not KMS key
  # material), but it should still be treated as sensitive: it maps out
  # exactly where the store lives and which roles can reach it.
  #
  # Provision the state bucket FIRST via deploy/aws/bootstrap/, then
  # uncomment this block.
  #
  # Real values (your state bucket name, which embeds your AWS account
  # ID) go in deploy/aws/backend.hcl - copy backend.hcl.example, fill it
  # in, and it's gitignored so those values never reach this public repo.
  # Init with:
  #   terraform init -backend-config=backend.hcl
  #
  backend "s3" {
    # The object key Terraform's own state lives under. Set it before the
    # first `init` and then leave it: changing it later does not move the
    # state file, it points Terraform at an object that does not exist,
    # so the next `init`/`plan` reads an EMPTY state. Keep it in step
    # with the state bucket name in bootstrap/main.tf.
    key          = "phi-ai/aws/terraform.tfstate"
    encrypt      = true
    use_lockfile = true # S3-native locking; no DynamoDB table needed
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "phi-ai"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Data classification tag drives cost allocation and, more
      # importantly, makes it obvious in the console which resources are
      # in PHI scope for audit purposes.
      DataClass = "phi"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}
# Made by Ryan Gomez & Co. Inc.
