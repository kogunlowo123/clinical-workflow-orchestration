terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.5" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
    cognitive_account {
      purge_soft_delete_on_destroy = false
    }
  }
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "cwo" {
  name = var.resource_group_name
}

locals {
  name     = var.name
  location = data.azurerm_resource_group.cwo.location
  # Storage, Key Vault and Cognitive Services names are global; derive a stable unique suffix.
  suffix = substr(sha1("${data.azurerm_client_config.current.subscription_id}${local.name}"), 0, 8)
}

# ------------------------------------------------------------------ database

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "azurerm_postgresql_flexible_server" "cwo" {
  name                          = "${local.name}-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.cwo.name
  location                      = local.location
  version                       = "16"
  sku_name                      = var.db_sku
  storage_mb                    = var.db_storage_mb
  delegated_subnet_id           = var.db_subnet_id
  private_dns_zone_id           = var.db_private_dns_zone_id
  public_network_access_enabled = false
  administrator_login           = "cwo"
  administrator_password        = random_password.db.result
  backup_retention_days         = 35
  geo_redundant_backup_enabled  = true
  zone                          = "1"
  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }
  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.cwo.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_configuration" "log_connections" {
  name      = "log_connections"
  server_id = azurerm_postgresql_flexible_server.cwo.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_database" "cwo" {
  name      = "cwo"
  server_id = azurerm_postgresql_flexible_server.cwo.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# ----------------------------------------------------------------- key vault

resource "azurerm_key_vault" "cwo" {
  name                          = "kv-cwo-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.cwo.name
  location                      = local.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "premium"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }
  tags = var.tags
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "${local.name}-database-url"
  key_vault_id = azurerm_key_vault.cwo.id
  value = format(
    "postgresql+asyncpg://cwo:%s@%s:5432/cwo?ssl=require",
    random_password.db.result, azurerm_postgresql_flexible_server.cwo.fqdn,
  )
  content_type    = "sqlalchemy-url"
  expiration_date = var.secret_expiration_date
}

# ------------------------------------------- Azure OpenAI (BAA-covered, private)

resource "azurerm_cognitive_account" "openai" {
  name                               = "oai-cwo-${local.suffix}"
  resource_group_name                = data.azurerm_resource_group.cwo.name
  location                           = local.location
  kind                               = "OpenAI"
  sku_name                           = "S0"
  custom_subdomain_name              = "oai-cwo-${local.suffix}"
  public_network_access_enabled      = false
  outbound_network_access_restricted = true
  network_acls {
    default_action = "Deny"
  }
  identity {
    type = "SystemAssigned"
  }
  tags = var.tags
}

resource "azurerm_cognitive_deployment" "care_plans" {
  name                 = var.openai_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.openai_model_name
    version = var.openai_model_version
  }
  sku {
    name     = "Standard"
    capacity = var.openai_capacity_ktpm
  }
}

resource "azurerm_key_vault_secret" "openai_key" {
  name            = "${local.name}-azure-openai-key"
  key_vault_id    = azurerm_key_vault.cwo.id
  value           = azurerm_cognitive_account.openai.primary_access_key
  content_type    = "api-key"
  expiration_date = var.secret_expiration_date
}

# ---------------------------------- FHIR service for synthetic test environments

resource "azurerm_healthcare_workspace" "cwo" {
  count               = var.create_fhir_service ? 1 : 0
  name                = "hdscwo${local.suffix}"
  resource_group_name = data.azurerm_resource_group.cwo.name
  location            = local.location
  tags                = var.tags
}

resource "azurerm_healthcare_fhir_service" "cwo" {
  count               = var.create_fhir_service ? 1 : 0
  name                = "fhircwo${local.suffix}"
  resource_group_name = data.azurerm_resource_group.cwo.name
  location            = local.location
  workspace_id        = azurerm_healthcare_workspace.cwo[0].id
  kind                = "fhir-R4"
  authentication {
    authority = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}"
    audience  = "https://hdscwo${local.suffix}-fhircwo${local.suffix}.fhir.azurehealthcareapis.com"
  }
  tags = var.tags
}

# ------------------------------------------------------------- audit (WORM)

resource "azurerm_storage_account" "audit" {
  name                            = "stcwoaudit${local.suffix}"
  resource_group_name             = data.azurerm_resource_group.cwo.name
  location                        = local.location
  account_tier                    = "Standard"
  account_replication_type        = "GZRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  public_network_access_enabled   = false
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }
  blob_properties {
    versioning_enabled = true
  }
  immutability_policy {
    allow_protected_append_writes = true
    state                         = "Unlocked"
    period_since_creation_in_days = var.audit_retention_days
  }
  tags = var.tags
}

resource "azurerm_storage_container" "audit" {
  name                  = "audit-chains"
  storage_account_id    = azurerm_storage_account.audit.id
  container_access_type = "private"
}

# ---------------------------------------------- workload identity (AKS)

resource "azurerm_user_assigned_identity" "cwo" {
  name                = local.name
  resource_group_name = data.azurerm_resource_group.cwo.name
  location            = local.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "cwo" {
  name                = "${local.name}-aks"
  resource_group_name = data.azurerm_resource_group.cwo.name
  parent_id           = azurerm_user_assigned_identity.cwo.id
  issuer              = var.aks_oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:${var.service_account}"
  audience            = ["api://AzureADTokenExchange"]
}

resource "azurerm_role_assignment" "audit_writer" {
  scope                = azurerm_storage_container.audit.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.cwo.principal_id
}

resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.cwo.principal_id
}
