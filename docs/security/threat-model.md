# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Patient record in the EHR | Unsafe care plans written under a clinician's name; integrity of the medical record |
| PHI in workflow state and model traffic | Breach notification, regulatory penalties, patient harm |
| Platform signing key | Forged sign-off attestations and audit events |
| SMART private key | Arbitrary reads and writes to the EHR within the registered scopes |
| Clinician identities and roles | Approvals by people not entitled to sign |
| Policy set and knowledge tables | Unsafe plans pass validation or approval |
| Audit chains | Loss of accountability for clinical decisions |

## Trust boundaries

1. People and agents → orchestrator API (untrusted network callers).
2. Orchestrator → EHR FHIR API (record content is untrusted input for models).
3. Orchestrator → model providers (PHI leaves the process).
4. Orchestrator → identity fabric and IdP JWKS.
5. Orchestrator → PostgreSQL, Key Vault, audit storage.

## STRIDE

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| T1 | Indirect prompt injection in chart free text steers drafting | Tampering / elevation | Record passed as EXTERNAL trust between random boundaries; injection detector blocks at score 0.5; workflow fails `security_violation` without a model call | `tests/security::test_t1_indirect_prompt_injection_in_record`, `tests/unit/test_agents.py::test_injection_in_record_is_blocked` |
| T2 | PHI disclosed to a model provider | Information disclosure | De-identified clinical view; RESTRICTED routing to BAA/in-tenant providers only; PII redaction | `test_t2_phi_minimisation_in_model_traffic`, `test_model_view_is_deidentified` |
| T3 | Model output causes an EHR write | Elevation | No write path from model output; commit requires signed approval (ADR-0001) | `test_t3_model_cannot_write_or_self_approve` |
| T4 | Hallucinated resources in a plan | Tampering | Grounding check: cited resources must exist in the evidence packet | `test_t4_hallucinated_references_are_errors`, `TestRules::test_grounding` |
| T5 | Signed plan swapped before commit | Tampering | JWS over the draft digest verified at commit; digest supplied by the signer | `test_t5_signed_plan_cannot_be_swapped`, `test_commit_rechecks_preconditions` |
| T6 | Agent impersonates a clinician to sign | Spoofing / elevation | Sign-off requires a clinician OIDC token (different issuer and audience); policy denies `careplan:*` for non-human subjects | `test_t6_agent_cannot_sign_off` |
| T7 | Forged, unsigned or tampered agent credentials | Spoofing | Signature verification against JWKS, single algorithm family, issuer/audience checks; `alg: none` rejected | `test_t7_forged_and_unsigned_agent_credentials`, `TestJwks` |
| T8 | PHI access without purpose or clearance | Information disclosure | Purpose of use required (TREAT/HOPERAT); classification ceiling from the credential | `test_t8_purpose_of_use_and_classification`, `TestAgentAuthorizers::test_local_mode` |
| T9 | SSRF and token leakage via FHIR paging links | Information disclosure | Next links followed only under the configured base URL | `test_t9_fhir_paging_ssrf`, `test_search_refuses_foreign_next_link` |
| T10 | Path or query injection through ids | Tampering | FHIR id grammar enforced before URLs; workflow id pattern | `test_t10_path_injection_in_ids` |
| T11 | Clinician signs past a safety error unknowingly | Repudiation / safety | Every error finding must be acknowledged with a reason; acknowledgements recorded in the signature and audit | `test_t11_unacknowledged_errors_block_signature`, `test_errors_must_be_acknowledged` |
| T12 | Audit tampering or silent audit failure | Repudiation | Hash-chained, Ed25519-signed events; audit write failure fails the operation | `test_t12_audit_chain_detects_tampering`, `tests/chaos::test_audit_failure_stops_the_step` |
| T13 | Duplicate EHR writes on retries | Tampering (record integrity) | Conditional creates, recover-before-write, no transaction retries at HTTP level | `tests/chaos::test_lost_commit_response_is_recovered_without_duplicates` |
| T14 | Denial of service via workflow floods or model cost | DoS | Auth on every endpoint, bounded runner concurrency, page limits, HPA, spend alert | `configs/prometheus/alerts.yaml` |
| T15 | Concurrent replicas double-advance a workflow | Tampering | Leases and optimistic concurrency | `test_lease_prevents_concurrent_advancement`, `TestRepository` |

## MITRE ATT&CK (Enterprise)

| Technique | Mitigation |
|---|---|
| T1078.004 Valid Accounts: Cloud Accounts | Workload identity, short-lived fabric credentials, Entra ID conditional access for clinicians |
| T1528 Steal Application Access Token | Tokens never persisted or logged; collector strips Authorization headers; paging SSRF guard |
| T1552.004 Unsecured Credentials: Private Keys | Keys only in Key Vault, mounted read-only (0400), never in Terraform state |
| T1565.001 Data Manipulation: Stored Data Manipulation | Signed sign-off, Provenance, hash-chained audit |
| T1562.008 Impair Defenses: Disable Cloud Logs | Immutable audit storage; audit failure fails closed |
| T1190 Exploit Public-Facing Application | Input validation, property-based API fuzzing in CI, SAST, image scanning |

## MITRE ATLAS

| Technique | Mitigation |
|---|---|
| AML.T0051.001 LLM Prompt Injection: Indirect | T1 controls |
| AML.T0054 LLM Jailbreak | Injection detector on all non-system content; outputs schema-validated |
| AML.T0057 LLM Data Leakage | De-identified inputs; output guard blocks credential material, redacts PII |
| AML.T0048 External Harms | Human sign-off; deterministic safety rules; advisory-only model review |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt Injection | Spotlighting, detector thresholds by trust level, fail-closed workflow |
| LLM02 Sensitive Information Disclosure | De-identification, RESTRICTED routing, output redaction |
| LLM03 Supply Chain | Pinned model versions (Terraform), SBOM, Grype, dependency audit |
| LLM04 Data and Model Poisoning | No fine-tuning on production data; knowledge tables under code review |
| LLM05 Improper Output Handling | JSON Schema validation; outputs never executed or written directly |
| LLM06 Excessive Agency | Agents propose only; writes need signed approval and policy allow |
| LLM07 System Prompt Leakage | Prompts contain no secrets or PHI; leakage has no security value |
| LLM08 Vector and Embedding Weaknesses | Not applicable: evidence comes from FHIR queries, not a vector store |
| LLM09 Misinformation | Grounding checks, deterministic rules, clinician review |
| LLM10 Unbounded Consumption | Max tokens per call, bounded retries and revisions, spend metrics and alert |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 Broken Object Level Authorization | Policy on every workflow action; PHI requires purpose and clearance |
| API2 Broken Authentication | Signed tokens only, issuer/audience/expiry checks, fail-closed JWKS |
| API3 Broken Object Property Level Authorization | `extra="forbid"` inputs; agent outputs are minimum-necessary summaries |
| API4 Unrestricted Resource Consumption | Paging limits, body validation, bounded concurrency, HPA |
| API5 Broken Function Level Authorization | Distinct roles for coordinators, signers, auditors; agents cannot sign |
| API6 Unrestricted Access to Sensitive Business Flows | Idempotency keys; revision limit; workflow start requires role or scope |
| API7 Server Side Request Forgery | Fixed FHIR base URL; paging links validated |
| API8 Security Misconfiguration | Production refuses ephemeral keys and unauthenticated FHIR; Helm `required` values; restricted Pod Security |
| API9 Improper Inventory Management | Published OpenAPI and schemas checked for drift in CI; Agent Card lists every skill |
| API10 Unsafe Consumption of APIs | FHIR responses type-checked; OperationOutcome summarized, not echoed |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | Clinicians acknowledge findings without reading them (automation bias) | Medium | High | Clinical safety officer | Reasons required per finding; override rate KPI reviewed monthly | Medium |
| R2 | Knowledge tables miss a contraindication | Medium | High | Pharmacy and therapeutics committee | Versioned tables, code-owner review, local formulary extension | Medium |
| R3 | Prompt-injection false positives delay discharges | Medium | Medium | Platform | Failure surfaced with code; resume after review; tune thresholds | Low |
| R4 | EHR outage blocks write-back | Medium | Medium | Integration team | Retries with backoff, idempotent commit, runbook | Low |
| R5 | Platform signing key compromise | Low | High | Security operations | Key Vault, rotation, JWKS publishes current key | Low |
| R6 | Model provider changes behaviour on upgrade | Medium | Medium | AI governance | Pinned model version; schema validation; deterministic rules unaffected | Low |
| R7 | Database outage | Low | High | Platform | Zone-redundant HA, PITR, readiness probe | Low |
