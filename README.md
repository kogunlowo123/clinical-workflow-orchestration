# Clinical Workflow Orchestration

Multi-agent orchestration of post-encounter care coordination on FHIR R4. For
an encounter, the service gathers the patient's record from the EHR, has a
care-coordination agent draft a care plan and task set, has a clinical
validation agent check it with deterministic safety rules and a model review,
routes it through policy to a clinician for sign-off, and only then writes the
CarePlan, Tasks and a signed Provenance back to the EHR.

```
Encounter ─▶ Evidence Retrieval ─▶ Care Coordination Agent ─▶ Clinical Validation Agent
          ─▶ Approval Workflow (policy) ─▶ Clinician Sign-off ─▶ EHR write-back + Provenance
```

Nothing reaches the patient record without an approving, signed clinician
decision. All data in this repository is synthetic.

## Capabilities

| Capability | Implementation |
|---|---|
| FHIR R4 | Async client: read, paged search (same-origin next links only), transactions; retries with backoff; OperationOutcome handling |
| SMART on FHIR | Backend services: client credentials with signed JWT assertion (RS384/ES384), discovery, token caching, 401 refresh |
| Evidence | Encounter, Patient, Condition, MedicationRequest (+Medication), Observation, AllergyIntolerance, CarePlan, Task; provenance digest per source; de-identified model view |
| Workflow engine | Persisted state machine, optimistic concurrency, leases, crash recovery sweep, bounded automatic retries, resumable and idempotent steps |
| Care-coordination agent | Drafts a typed `CarePlanProposal` via agentic-core ModelRouter (Azure OpenAI primary, in-cluster vLLM fallback), ContentGuard in and out, schema validation with one corrective turn |
| Clinical validation agent | Grounding, duplicate therapy, interactions, allergy contraindication and cross-reactivity, follow-up timing, abnormal-result rechecks, metformin renal dosing; advisory model review capped at warning |
| Approval | agentic-core PolicyEngine (`policies/clinical-workflow.yaml`); clinicians acknowledge every error finding with a reason; drafts are signed by digest |
| Write-back | One FHIR transaction: CarePlan + Tasks (conditional create) + Provenance with Verification Signature (Ed25519 JWS); recovery after lost responses |
| Agent interop | MCP tools (official SDK, streamable HTTP at `/mcp`), A2A Agent Card and JSON-RPC (`/a2a`), Microsoft Agent Framework adapter |
| Identity | Clinicians via enterprise OIDC (`roles`, `fhirUser`); agents via the [agent identity fabric](https://github.com/kogunlowo123/agent-identity-fabric) (jwt-bearer, introspection, authorize) or local policy |
| Audit and observability | Signed hash-chained audit, Prometheus metrics and alerts, Grafana dashboard, OpenTelemetry traces, correlation IDs |

Shared controls (model routing, policy, audit, content guard, telemetry) come
from [agentic-core](https://github.com/kogunlowo123/agentic-core).

## Quick start (local)

Requires Docker. No API keys or cloud credentials.

```bash
docker compose up --build
```

The stack runs PostgreSQL, HAPI FHIR seeded with a synthetic heart-failure
discharge (`deployments/docker/fhir-seed/`), Ollama with a local model, an OIDC
provider standing in for the clinician IdP and the agent credential issuer,
and an OpenTelemetry collector.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs CWO_TEST_DATABASE_URL)
make helm terraform
```

Test suites: `tests/unit`, `tests/security` (attacks mapped to the threat
model), `tests/chaos` (EHR, model, database, audit and commit failures),
`tests/performance` (latency budgets; `locustfile.py` for load), `tests/e2e`
(REST, A2A and MCP lifecycles and a property-based OpenAPI test),
`tests/integration` (PostgreSQL). The FHIR server, model endpoints and
identity providers are in-process fakes that speak the real wire formats.

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/workflows` | Care coordinator (`Idempotency-Key` supported) | Start the workflow for an encounter |
| `GET /v1/workflows`, `GET /v1/workflows/{id}` | Coordinator, clinician, auditor | Worklist and full workflow view (evidence, draft, findings, history) |
| `POST /v1/workflows/{id}/decision` | Clinician (`clinician-approver`, `fhirUser`) | Approve, reject or request changes; acknowledge findings |
| `POST /v1/workflows/{id}/resume`, `/cancel` | Coordinator | Resume a failed step; cancel |
| `POST /v1/medication-checks` | Coordinator, clinician | Check proposed medication changes against the record |
| `GET /.well-known/agent-card.json`, `POST /a2a` | Agents | A2A discovery and JSON-RPC |
| `POST /mcp` | Agents | MCP tools |
| `GET /.well-known/jwks.json` | Anyone | Key verifying sign-off signatures in Provenance |
| `/healthz`, `/readyz`, `/metrics` | Platform | Liveness, readiness (database), Prometheus |

Contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/clinical-workflow-orchestration` (StatefulSet, PDB, HPA, NetworkPolicy, ServiceMonitor, projected fabric token) |
| Namespace and secrets | `deployments/kubernetes` (restricted Pod Security, External Secrets) |
| Cloud | `deployments/terraform/azure` (primary: PostgreSQL, Key Vault, Azure OpenAI, immutable audit storage, Workload Identity, optional Health Data Services FHIR), `aws`, `gcp` |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md), [agent specification](docs/architecture/agent-specification.md), [agent interoperability](docs/architecture/agent-interoperability.md), [cloud reference](docs/architecture/cloud-reference.md)
- [ADRs](docs/architecture/adr/)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md), [key rotation](docs/runbooks/key-rotation.md), [clinical safety review](docs/runbooks/clinical-safety-review.md)
- [Configuration reference](docs/operations/configuration.md)

## License

Apache-2.0
