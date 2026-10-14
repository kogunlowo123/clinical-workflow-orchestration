# Cloud reference implementation

Azure is the primary reference deployment; Terraform for AWS and GCP provides
the same capabilities with native services. The application is identical on
all three; only Helm values and `configs/providers.yaml` change.

| Capability | Azure (primary) | AWS | GCP |
|---|---|---|---|
| Kubernetes | AKS with Workload Identity | EKS with IRSA | GKE with Workload Identity |
| Database | PostgreSQL Flexible Server, zone-redundant HA, geo-redundant backups, TLS required | RDS PostgreSQL Multi-AZ, KMS, forced SSL, IAM auth enabled | Cloud SQL PostgreSQL regional, CMEK, private IP, SSL only |
| Secrets | Key Vault (premium, RBAC, purge protection, private) | Secrets Manager with KMS | Secret Manager with regional replication |
| Audit storage | Immutable Blob (GZRS, versioning, time-based retention) | S3 Object Lock, compliance mode | GCS bucket retention policy, optional lock |
| Models | Azure OpenAI (private, BAA) + in-cluster vLLM | In-cluster vLLM (or Bedrock through an OpenAI-compatible gateway approved for PHI) | In-cluster vLLM (or Vertex AI through an approved gateway) |
| FHIR | EHR FHIR API; optional Azure Health Data Services FHIR R4 service for synthetic environments | EHR FHIR API (AWS HealthLake for synthetic environments) | EHR FHIR API (Cloud Healthcare API for synthetic environments) |

Terraform lives in `deployments/terraform/{azure,aws,gcp}`. It never creates
signing or SMART keys (they would land in state); keys are generated offline
and written to the secret manager (runbook `startup.md`). Database passwords
are random and stored in the secret manager; they do exist in state, so state
must be in an encrypted, access-controlled backend.

## Azure topology

- Private networking: PostgreSQL in a delegated subnet; Key Vault, Storage and
  Azure OpenAI with public network access disabled and private endpoints.
- Identities: one user-assigned managed identity federated to the
  `clinical-workflow/clinical-workflow-orchestration` service account, granted
  Blob Data Contributor on the audit container and Cognitive Services OpenAI
  User on the OpenAI account.
- EHR connectivity: private link or VPN to the EHR's FHIR endpoint; the SMART
  client's public key is registered with the EHR.

## Scaling

| Dimension | Approach |
|---|---|
| API and agent traffic | HPA 3–10 replicas on CPU; stateless request handling |
| Workflow execution | Database leases; per-replica concurrency limit (`CWO_RUNNER_CONCURRENCY`) |
| EHR rate limits | Exponential backoff with jitter; paging limits (`CWO_FHIR_MAX_PAGES`) |
| Model capacity | Azure OpenAI TPM quota sized from expected discharges per hour; fallback absorbs bursts |
| Database | Workflow rows are small JSON documents; indexes on state, encounter and next attempt |

## Cost model (inputs, not estimates)

Monthly cost = AKS nodes + PostgreSQL SKU + storage + Key Vault operations +
model tokens. Model tokens scale with discharges × revisions × two calls. The
service exports the per-call cost it computes from contracted prices
(`CWO_AZURE_OPENAI_INPUT_COST_PER_MTOK`, `..._OUTPUT_...`), so finance sees the
real figure per workflow on the dashboard instead of an estimate in a document.
