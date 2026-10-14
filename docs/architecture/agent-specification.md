# Agent specification

Three agents cooperate under one orchestrator. Each is registered in the agent
identity fabric with the mandate below; this document is the source for those
registrations.

## 1. Clinical workflow orchestrator

| Item | Specification |
|---|---|
| Purpose | Run the post-encounter care-coordination workflow end to end and own its state |
| Business objective | Every discharge gets a grounded, validated, clinician-signed care plan in the EHR within the same shift |
| Protected resources | Patient record (FHIR), workflow state, signing key, audit evidence |
| Operating boundaries | Writes to the EHR only after an approving sign-off with a valid signature; writes only CarePlan, Task, Provenance; never MedicationRequest |
| Delegated responsibility | Drafting to the care-coordination agent, review to the clinical-validation agent; decisions to policy and the signing clinician |
| Identity | SPIFFE id `spiffe://<domain>/agent/clinical/orchestrator`; credential from the identity fabric (RFC 7523) using a projected Kubernetes token |
| Risk tier | HIGH (writes to the medical record) |

## 2. Care-coordination agent

| Item | Specification |
|---|---|
| Purpose | Draft a post-encounter CarePlan and Task set from the evidence packet |
| Inputs | De-identified evidence view: age band, sex, encounter class and period, active conditions, active medications, recent labs (latest per analyte), allergies, open CarePlans and Tasks; clinician feedback on revisions |
| Output | `CarePlanProposal` (`schemas/care-plan-proposal.model-output.schema.json`): title, goals, up to 25 typed activities with due dates, priorities, evidence references and medication proposals |
| Allowed | Propose follow-up, labs, referrals, monitoring, education; propose medication start/stop/continue/adjust for clinician review |
| Disallowed | Writing anything; diagnosing; prescribing; citing resources outside the record (detected as `grounding` errors) |
| Model access | ModelRouter task `care-plan-draft`, classification RESTRICTED, temperature 0, ContentGuard on input and output, one corrective turn on schema failure |
| Risk tier | MODERATE (output is a proposal; every item is validated and signed) |

## 3. Clinical-validation agent

| Item | Specification |
|---|---|
| Purpose | Find safety problems in a drafted plan before a clinician sees it |
| Deterministic checks (authoritative) | grounding, duplicate therapy (ingredient and class), clinically significant interactions, allergy contraindication and cross-reactivity, missing condition and post-discharge follow-up, abnormal results without timely recheck, metformin against eGFR |
| Model review (advisory) | Concerns rules cannot express; severity capped at WARNING; activity indexes validated |
| Output | `ValidationReport` (`schemas/validation-report.schema.json`) with stable finding ids, ruleset version, review status and model provenance |
| Degradation | If every model provider is unavailable the deterministic report is produced and `review_status=unavailable` is shown to the clinician |
| Risk tier | MODERATE |

## Delegated scope

| Action | Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|---|
| Read the patient record | Orchestrator, for the encounter's patient, purpose TREAT | Bulk export, other patients | MODERATE | Denied PHI access is audited and counted |
| Draft a plan | Care-coordination agent | Writes, diagnosis, prescribing | MODERATE | Schema failure fails the step; clinician never sees invalid output |
| Validate | Clinical-validation agent | Clearing deterministic errors | MODERATE | Errors require clinician acknowledgement with a reason |
| Present for approval | Orchestrator, when policy returns `require_approval` | Presenting a plan policy denies | HIGH | Workflow fails with `policy_denied` |
| Approve / reject / return | People with role `clinician-approver` and a `fhirUser` Practitioner | Agents, coordinators, the drafting identity | HIGH | Denials audited; alert on spikes |
| Write to EHR | Orchestrator, after signed approval and policy `allow` (and fabric not `deny`) | Any write without signature; MedicationRequest | HIGH | Failure leaves the workflow `failed`, retried safely |
| Agent skills (A2A/MCP) | Agents with matching scopes, purpose TREAT/HOPERAT, RESTRICTED clearance | Delegated agents starting workflows; signing | MODERATE | 403 / JSON-RPC -32010, audited |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of API requests | 99.9% over 30 days |
| SLO latency | p95 of workflow reads | < 300 ms |
| SLO timeliness | Workflows reaching `awaiting_approval` within 5 minutes of start | 95% |
| SLO overhead | Platform overhead per workflow excluding EHR and model latency | < 1 s (performance test) |
| Safety invariant | EHR writes without approving signed sign-off | 0 (enforced in code, policy and tests) |
| SLI | `cwo:request_errors:ratio_5m`, request and step duration histograms, `cwo_workflow_failures_total` | Prometheus |
| KPI | Share of discharges with a signed plan within 24 h | Reported from `cwo_workflow_transitions_total` |
| KPI | Clinician override rate (acknowledged errors / errors) | Reviewed monthly by the clinical safety group |
| KPI | Model spend per completed workflow | `cwo_model_cost_usd_total` / completions |
| Completion criteria | A workflow is complete when the transaction-response is recorded with CarePlan, Task and Provenance references and the transition is in the signed audit chain | Enforced: audit failure fails the operation |

## Permissions

| Mechanism | Use |
|---|---|
| RBAC | Entra ID app roles `care-coordinator`, `clinician-approver`, `clinical-auditor` |
| ABAC | `policies/clinical-workflow.yaml`: purpose of use, classification, unacknowledged errors, drafter, signer, subject kind |
| Identity | Clinicians: OIDC with `fhirUser`; agents: SPIFFE ids with fabric credentials; the orchestrator: workload identity |
| OAuth / OIDC | Bearer tokens (RFC 6750); jwt-bearer (RFC 7523) and introspection (RFC 7662) at the fabric; SMART backend services (client credentials + signed JWT assertion) at the EHR |
| Workload identity | AKS Workload Identity for Azure resources; projected service-account token for the fabric |

## Inputs, outputs and validation

All request and response bodies are Pydantic models with published JSON
Schemas in `schemas/` and the OpenAPI document in `docs/api/openapi.json`
(`python -m clinical_workflow_orchestration.contracts --check` fails CI if they
drift). Inputs reject unknown fields; FHIR ids must match the R4 id grammar
before they reach a URL; workflow ids are 32 hex characters; draft digests are
`sha256:<64 hex>`.

## Provenance and audit metadata

- Every source FHIR resource used is recorded with its reference, version and
  SHA-256 digest in the evidence packet; the packet digest is bound into the
  draft digest the clinician signs.
- Model outputs carry provider, model, request ids, token counts, cost,
  attempts and output digest.
- Sign-off produces an Ed25519 JWS over workflow id, draft digest,
  practitioner, decision, acknowledged findings and token id; it is stored on
  the workflow and embedded in the FHIR Provenance (`signature.type`
  1.2.840.10065.1.12.1.5, Verification Signature). The public key is at
  `/.well-known/jwks.json`.
- Provenance names the drafting agent (author), the practitioner (verifier) and
  the orchestrator (assembler), with the evidence as source entities.
- Audit events (agentic-core AuditLog) carry actor, action, resource, outcome,
  correlation id, policy digest and matched rules; they are hash-chained and
  signed per replica.
