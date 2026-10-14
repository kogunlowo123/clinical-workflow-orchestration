# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Persisted, resumable clinical workflow state machine with leases, optimistic
  concurrency, bounded automatic retries and crash recovery.
- FHIR R4 client (read, paged search with same-origin next links, transactions)
  and SMART backend-services authorization with signed JWT client assertions
  (RS384, ES384).
- Evidence retrieval for Encounter, Patient, Condition, MedicationRequest
  (including Medication references), Observation, AllergyIntolerance, CarePlan
  and Task, with per-resource provenance digests and a de-identified model view.
- Care-coordination agent drafting typed CarePlan proposals through the
  agentic-core ModelRouter with ContentGuard and schema validation.
- Clinical-validation agent: grounding, duplicate therapy, interactions,
  allergy contraindication and cross-reactivity, condition and post-discharge
  follow-up, abnormal-result rechecks and metformin renal dosing, plus advisory
  model review.
- Policy-gated clinician sign-off (approve, reject, request changes) with
  per-finding acknowledgements and an Ed25519-signed attestation.
- Idempotent EHR write-back of CarePlan, Tasks and Provenance (verification
  signature) in one transaction, with recovery after lost responses.
- Agent identity fabric client (jwt-bearer, introspection, authorization) and
  local policy fallback for inbound agent authorization.
- MCP tools (official SDK, streamable HTTP), A2A Agent Card and JSON-RPC
  endpoint, Microsoft Agent Framework adapter.
- Prometheus metrics, alert rules, Grafana dashboard, OpenTelemetry traces,
  signed audit chain.
- Helm chart, Kubernetes namespace and secret sync, Terraform for Azure
  (primary), AWS and GCP; CI and release workflows.
