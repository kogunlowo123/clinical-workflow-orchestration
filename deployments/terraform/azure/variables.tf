variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "clinical-workflow-orchestration"
}

variable "subscription_id" {
  description = "Azure subscription"
  type        = string
}

variable "resource_group_name" {
  description = "Existing resource group"
  type        = string
}

variable "db_subnet_id" {
  description = "Subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers"
  type        = string
}

variable "db_private_dns_zone_id" {
  description = "Private DNS zone privatelink.postgres.database.azure.com linked to the VNet"
  type        = string
}

variable "db_sku" {
  description = "PostgreSQL Flexible Server SKU"
  type        = string
  default     = "GP_Standard_D4ds_v5"
}

variable "db_storage_mb" {
  description = "Database storage"
  type        = number
  default     = 131072
}

variable "openai_deployment_name" {
  description = "Azure OpenAI deployment name (CWO_AZURE_OPENAI_DEPLOYMENT)"
  type        = string
  default     = "care-plans"
}

variable "openai_model_name" {
  description = "Model deployed for care-plan drafting and clinical review"
  type        = string
  default     = "gpt-4o"
}

variable "openai_model_version" {
  description = "Model version; pin it so behaviour changes are deliberate"
  type        = string
  default     = "2024-11-20"
}

variable "openai_capacity_ktpm" {
  description = "Deployment capacity in thousands of tokens per minute"
  type        = number
  default     = 50
}

variable "create_fhir_service" {
  description = "Create an Azure Health Data Services FHIR R4 service for synthetic-data environments"
  type        = bool
  default     = false
}

variable "aks_oidc_issuer_url" {
  description = "AKS cluster OIDC issuer URL (az aks show --query oidcIssuerProfile.issuerUrl)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace of the orchestrator"
  type        = string
  default     = "clinical-workflow"
}

variable "service_account" {
  description = "Kubernetes service account of the orchestrator"
  type        = string
  default     = "clinical-workflow-orchestration"
}

variable "audit_retention_days" {
  description = "Immutability period for audit chains (HIPAA documentation retention: 6 years minimum)"
  type        = number
  default     = 2555
}

variable "secret_expiration_date" {
  description = "RFC 3339 expiry for Key Vault secrets; rotate before this date"
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default     = { "app.kubernetes.io/part-of" = "clinical-workflow-orchestration" }
}
