# Configuration reference

All settings are `CWO_*` environment variables (`src/clinical_workflow_orchestration/config.py`).
The service refuses to start with incomplete or unsafe configuration
(production forbids ephemeral keys, plain-HTTP FHIR, unauthenticated FHIR and
running without an agent-credential verifier).

| Variable | Required | Default | Description |
|---|---|---|---|
| `CWO_ENVIRONMENT` | no | `production` | `production` enables the strict checks above |
| `CWO_PUBLIC_URL` | yes | | Public base URL; used in the A2A Agent Card |
| `CWO_DATABASE_URL` | yes | | SQLAlchemy async URL (`postgresql+asyncpg://...`) |
| `CWO_FHIR_BASE_URL` | yes | | EHR FHIR R4 base URL |
| `CWO_SMART_CLIENT_ID` | prod | | SMART backend-services client id |
| `CWO_SMART_PRIVATE_KEY_FILE` | prod | | PEM private key (RS384: RSA ≥ 2048; ES384: P-384) |
| `CWO_SMART_KEY_ID` | prod | | kid registered with the EHR |
| `CWO_SMART_ALGORITHM` | no | `RS384` | `RS384` or `ES384` |
| `CWO_SMART_TOKEN_URL` | no | discovered | Token endpoint; else `.well-known/smart-configuration` |
| `CWO_IDENTIFIER_SYSTEM` | no | `urn:ietf:rfc:3986` | Identifier system for CarePlan/Task identifiers |
| `CWO_FHIR_MAX_PAGES` | no | `20` | Search paging limit |
| `CWO_FHIR_TIMEOUT_SECONDS` | no | `20` | HTTP timeout for FHIR, SMART, JWKS, fabric |
| `CWO_PROVIDERS_FILE` | no | `configs/providers.yaml` | Model providers; `${VARS}` resolved from the environment |
| `CWO_MODEL_MAX_TOKENS` | no | `2048` | Max output tokens per model call |
| `CWO_POLICY_FILE` | no | `policies/clinical-workflow.yaml` | Governance policy |
| `CWO_SIGNING_KEY_FILE` | yes* | | Ed25519 PEM (sign-offs and audit); kid = file stem |
| `CWO_DEV_EPHEMERAL_KEYS` | no | `false` | In-memory key; non-production only |
| `CWO_AUDIT_LOG_PATH` | no | `/var/lib/clinical-workflow/audit/audit.jsonl` | Audit chain file |
| `CWO_AGENT_ID` | yes | | SPIFFE id of the orchestrator |
| `CWO_DRAFTING_AGENT_ID` / `CWO_VALIDATION_AGENT_ID` | no | derived | Ids recorded for the two agents |
| `CWO_USER_ISSUER` / `CWO_USER_AUDIENCE` / `CWO_USER_JWKS_URL` | yes | | Clinician IdP |
| `CWO_USER_ROLES_CLAIM` | no | `roles` | Claim with app roles |
| `CWO_USER_FHIR_USER_CLAIM` | no | `fhirUser` | Claim with the Practitioner reference |
| `CWO_IDENTITY_FABRIC_URL` | prod** | | Identity fabric; enables fabric authorization and own credential |
| `CWO_WORKLOAD_TOKEN_FILE` | with fabric | | Projected service-account token |
| `CWO_AGENT_TOKEN_ISSUER` / `_AUDIENCE` / `CWO_AGENT_JWKS_URL` | prod** | | Local mode: verifier for agent credentials |
| `CWO_MAX_REVISIONS` | no | `3` | Clinician change requests per workflow |
| `CWO_LEASE_SECONDS` | no | `300` | Workflow lease duration |
| `CWO_SWEEP_INTERVAL_SECONDS` | no | `30` | Recovery sweep interval |
| `CWO_MAX_AUTO_RETRIES` / `CWO_RETRY_BASE_SECONDS` | no | `3` / `30` | Automatic retry budget and backoff base |
| `CWO_RUNNER_CONCURRENCY` | no | `8` | Concurrent workflow steps per replica |
| `CWO_LOG_LEVEL` | no | `INFO` | |
| `CWO_OTLP_ENDPOINT` | no | | OTLP gRPC endpoint for traces |
| `CWO_HOST` / `CWO_PORT` | no | `127.0.0.1` / `8080` | The image sets host `0.0.0.0` |
| `CWO_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |

\* unless `CWO_DEV_EPHEMERAL_KEYS=true` outside production.
\** production requires the identity fabric or the local agent verifier.

Provider variables referenced by `configs/providers.yaml`:
`CWO_AZURE_OPENAI_ENDPOINT`, `CWO_AZURE_OPENAI_DEPLOYMENT`,
`CWO_AZURE_OPENAI_MODEL`, `CWO_AZURE_REGION`,
`CWO_AZURE_OPENAI_INPUT_COST_PER_MTOK`, `CWO_AZURE_OPENAI_OUTPUT_COST_PER_MTOK`,
`CWO_AZURE_OPENAI_API_KEY`, `CWO_SELF_HOSTED_LLM_URL`, `CWO_SELF_HOSTED_MODEL`.
