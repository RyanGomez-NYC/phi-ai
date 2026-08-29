terraform {
  required_version = ">= 1.10.0" # matches deploy/aws/versions.tf - no Azure-specific reason to differ

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0" # v4.x - the azurerm_storage_container_immutability_policy resource used in
      # storage.tf until immutability was removed, and its documented behavior, were
      # confirmed current against the v4.75.0 provider docs (registry.terraform.io) as of
      # this writing. Pin conservatively and re-check the provider's own changelog before
      # bumping past 4.x, the same discipline deploy/aws/versions.tf applies to the aws
      # provider.
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6" # matches deploy/aws/versions.tf
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11" # used by keyvault.tf's time_sleep, to absorb Azure RBAC
      # role-assignment propagation delay before the deploying principal's new
      # Key Vault Crypto Officer grant is used to create a key - a well-documented
      # eventual-consistency gap between "the role assignment object exists" and
      # "Azure's authorization cache has actually picked it up."
    }
  }

  # Remote state backend. See deploy/azure/bootstrap/ for the one-time setup this
  # references - provision that FIRST. Mirrors deploy/aws/versions.tf's own
  # chicken-and-egg reasoning exactly: the storage account holding this
  # stack's state has to exist before Terraform can use it as a backend.
  #
  # This block is intentionally active (not commented out) with only `key`
  # set - a standard Terraform "partial backend configuration." The three
  # remaining required arguments (resource_group_name, storage_account_name,
  # container_name) come from deploy/azure/backend.hcl - copy
  # backend.hcl.example, fill it in with bootstrap's own outputs; it's
  # gitignored so those values (which embed your Azure subscription ID)
  # never reach this public repo. Init with:
  #   terraform init -backend-config=backend.hcl
  backend "azurerm" {
    # The blob name Terraform's own state lives under inside the state
    # container. Set it before the first `init` and then leave it:
    # changing it later does not move the state blob, it points Terraform
    # at a blob that does not exist, so the next `init`/`plan` reads an
    # EMPTY state. Keep it in step with the state storage account name in
    # bootstrap/main.tf.
    key = "phi-ai/azure/terraform.tfstate"
    # resource_group_name, storage_account_name, container_name go in backend.hcl
  }
}

provider "azurerm" {
  features {
    key_vault {
      # Azure Key Vault soft-delete is on by default and cannot be disabled -
      # this setting only controls what THIS PROVIDER does on `terraform destroy`,
      # not whether soft-delete itself exists. Purging on destroy matters for a
      # DEV stack you want to actually tear down (see "Tearing down the dev
      # stack" in runbooks/RUNBOOK_AZURE_SETUP.md) - a soft-deleted vault's name
      # stays reserved for up to 90 days otherwise, which is exactly the kind
      # of thing that trips up re-running `terraform apply` with the same
      # resource names during iterative dev work.
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      # Unconditional, not gated by var.environment - unlike force_destroy_buckets'
      # AWS-side cost/convenience trade-off, this is a plain safety net ("don't let
      # a mistaken `terraform destroy` silently remove a group that still holds
      # resources") with no corresponding reason to ever want it off, dev or prod.
      prevent_deletion_if_contains_resources = true
    }
  }
}

data "azurerm_client_config" "current" {}
# Made by Ryan Gomez & Co. Inc.
