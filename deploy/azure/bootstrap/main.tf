# ---------------------------------------------------------------------------
# Terraform state backend bootstrap.
#
# Chicken-and-egg: the Azure Storage account that stores state must itself
# exist before you can use it as a backend. Run this ONCE, with local state,
# then configure the backend block in ../versions.tf and migrate. Mirrors
# deploy/aws/bootstrap/main.tf's identical reasoning and structure exactly.
#
# Keep this stack's own state file (terraform.tfstate in this directory)
# somewhere safe -- it is small and rarely changes, but losing it means
# losing Terraform's record of the state storage account.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "azure_location" {
  type    = string
  default = "eastus"
}

data "azurerm_client_config" "current" {}

# Storage account names must be globally unique across ALL of Azure (not just
# your subscription), lowercase alphanumeric only, 3-24 characters. A random
# suffix avoids a naming collision with someone else's account on first apply
# - unlike the AWS bootstrap, which can fold the account ID directly into the
# bucket name (S3 bucket names only need to be unique within the constraints
# S3 itself enforces, and the account ID already guarantees that), Azure
# storage account names have a much shorter length ceiling (24 chars) that
# doesn't comfortably fit a product name plus "tfstate" plus a full
# subscription GUID the way deploy/aws/bootstrap/main.tf's bucket name
# comfortably fits a 12-digit AWS account ID.
resource "random_string" "tfstate_suffix" {
  length  = 8
  special = false
  upper   = false
}

resource "azurerm_resource_group" "tfstate" {
  # Holds the storage account holding TERRAFORM'S OWN STATE - see the
  # comment on azurerm_storage_account.tfstate below for what changing
  # any of these names costs once the main stack has initialized against
  # them.
  name     = "phiai-tfstate"
  location = var.azure_location
}

resource "azurerm_storage_account" "tfstate" {
  # This account does not hold PHI; it holds TERRAFORM'S OWN STATE, which
  # makes its name a different kind of commitment than a data resource's.
  # Once the main stack has been initialized against it, changing this
  # string does not move the state blob - it points Terraform at an
  # account/container that does not exist, so the next `init`/`plan` for
  # the MAIN stack reads an EMPTY state and offers to CREATE a second
  # copy of every storage account, key, identity and server, while the
  # real ones keep existing, now unmanaged, still holding PHI and still
  # billing. Set it before the first init, then leave it - and keep the
  # backend `key` in ../versions.tf in step with it.
  #
  # THE LENGTH CONSTRAINT IS LIVE, not a historical anecdote. Azure
  # storage account names are a hard 3-24 characters, lowercase
  # alphanumeric only. The fixed portion below is "phiaitfstate" (12
  # characters) and the random suffix adds 8, for 20/24. An earlier
  # revision of this file used a 17-character fixed portion, which with
  # the same suffix totalled 25 and would have failed `terraform apply`
  # on the very first bootstrap attempt with a validation error, not a
  # warning. Any future rename has to re-solve the same budget.
  name                = "phiaitfstate${random_string.tfstate_suffix.result}"
  resource_group_name = azurerm_resource_group.tfstate.name
  location            = azurerm_resource_group.tfstate.location

  account_tier             = "Standard"
  account_replication_type = "LRS" # locally-redundant: cheapest option; state is small and re-derivable from the main stack's own resources if truly lost, unlike the stored PHI itself

  # State is not PHI, but it maps the whole deployment - same reasoning as
  # deploy/aws/bootstrap/main.tf's aws_s3_bucket.tfstate. TLS 1.2 floor
  # enforced regardless, matching the AWS bootstrap bucket's own
  # DenyInsecureTransport statement in spirit. https_traffic_only_enabled
  # defaults to true already, set explicitly anyway to match this
  # project's convention of never relying on a default for a
  # security-relevant setting, even a correct one.
  https_traffic_only_enabled = true
  min_tls_version             = "TLS1_2"

  # FIXED: this was public_network_access_enabled = false, which does not
  # mean what the original comment here claimed ("public access blocked,
  # matching the AWS bootstrap bucket's DenyInsecureTransport statement in
  # spirit"). AWS's public-access-block controls are about anonymous ACLs
  # and bucket policies - they leave the bucket reachable over the public
  # internet for any authenticated, authorized IAM caller, which is
  # exactly how core/storage/aws_s3.py and the AWS backend both connect
  # from an ordinary dev laptop with no VPN or VPC endpoint involved.
  # public_network_access_enabled is a different, stronger control: it
  # disables the storage account's public DATA-PLANE endpoint entirely
  # (blob.core.windows.net), and only the data plane - not Terraform's own
  # management-plane calls that create the account in the first place -
  # is what the azurerm backend needs to read and write the state blob.
  # Set to false with no Private Endpoint/VNet configured anywhere in this
  # file, `terraform apply` here would still succeed (management plane,
  # unaffected), but the very next step - `terraform init
  # -backend-config=backend.hcl` in the main stack - would fail to reach
  # the state container at all, from a completely ordinary dev laptop.
  # allow_nested_items_to_be_public below is the setting that actually
  # corresponds to what the original comment intended: it stops any
  # individual blob or container from being marked anonymously-readable,
  # without touching whether authenticated callers can reach the account
  # over the internet at all. Network-level restriction (Private
  # Endpoints, disabling public access) is exactly the kind of control
  # runbooks/RUNBOOK_AWS_SETUP.md's own "Promoting to production"
  # section calls out as a later, deliberate hardening step - not
  # something that should silently make the dev stack unreachable by
  # default.
  public_network_access_enabled = true
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

output "state_storage_account_name" {
  value       = azurerm_storage_account.tfstate.name
  description = "Set as `storage_account_name` in the backend block in ../versions.tf / backend.hcl."
}

output "state_resource_group_name" {
  value       = azurerm_resource_group.tfstate.name
  description = "Set as `resource_group_name` in backend.hcl."
}

output "state_container_name" {
  value       = azurerm_storage_container.tfstate.name
  description = "Set as `container_name` in backend.hcl."
}
# Made by Ryan Gomez & Co. Inc.
