output "workload_identity_client_id" {
  description = "Annotate the service account with azure.workload.identity/client-id = this value"
  value       = azurerm_user_assigned_identity.cwo.client_id
}

output "key_vault_uri" {
  description = "Key Vault holding the database URL, Azure OpenAI key, signing and SMART keys"
  value       = azurerm_key_vault.cwo.vault_uri
}

output "azure_openai_endpoint" {
  description = "Set as cwo.models.azureOpenAI.endpoint"
  value       = azurerm_cognitive_account.openai.endpoint
}

output "azure_openai_deployment" {
  description = "Set as cwo.models.azureOpenAI.deployment"
  value       = azurerm_cognitive_deployment.care_plans.name
}

output "fhir_service_url" {
  description = "FHIR R4 endpoint of the synthetic-data FHIR service, when created"
  value       = var.create_fhir_service ? "https://${azurerm_healthcare_workspace.cwo[0].name}-${azurerm_healthcare_fhir_service.cwo[0].name}.fhir.azurehealthcareapis.com" : null
}

output "audit_container" {
  description = "Immutable blob container receiving audit chains"
  value       = "${azurerm_storage_account.audit.name}/${azurerm_storage_container.audit.name}"
}
