# Runbook: incident response

Severity: page alerts are SEV2 unless PHI exposure or an unsigned EHR write is
suspected (SEV1, involve the privacy officer and clinical safety officer).

## Elevated 5xx

1. `kubectl -n clinical-workflow logs statefulset/cwo-clinical-workflow-orchestration --since=15m | jq 'select(.level=="error")'`
2. Check `/readyz` on each pod; database failures show as `database unavailable`.
3. If a release caused it, roll back (`rollback.md`).

## Step failures

1. Group failures: `sum by (step, code) (increase(cwo_workflow_failures_total[1h]))`.
2. `fhir_error` / `smart_auth_failed`: see EHR unavailable.
3. `provider_unavailable` / `model_output_invalid`: see model outage.
4. `policy_denied`: a policy change is denying presentation for approval;
   compare the `policy_digest` in recent audit events with the expected file.
5. Retryable failures retry automatically up to `CWO_MAX_AUTO_RETRIES`; after
   the incident, resume the rest: `POST /v1/workflows/{id}/resume`.

## EHR unavailable

Workflows queue as retryable failures; nothing is lost. Confirm with the EHR
team, then resume. For SMART token failures check the client registration and
that the registered public key matches `smart.pem` (key rotation runbook).

## Model outage

The router fails over to the in-cluster model automatically. If both fail,
drafting fails retryable. Validation still produces deterministic findings
with `review_status=unavailable`. Check provider status, quota and the vLLM
deployment.

## Security violation

`code=security_violation` means record content or model output was blocked by
the content guard.

1. Identify the workflow and encounter from the audit event
   (`action=workflow.failed`, `details.failure=security_violation`).
2. Review the source resources (listed in the workflow evidence sources) with
   the clinical informatics team; injected instructions in free text are a
   security event: preserve evidence, notify security operations.
3. If benign (false positive), correct or annotate the record and resume; do
   not lower guard thresholds without an ADR.

## Sign-off denials

Repeated `careplan:approve` denials: check the user's app roles, the `fhirUser`
claim mapping, and whether they are acknowledging all error findings.

## Agent denials

A deny spike means an agent lacks scopes, omits `purpose_of_use`, has a
lower classification ceiling, or credentials are being misused. Check
`agent.*` audit events for the caller and rules; suspend the agent in the
identity fabric if misuse is suspected.

## Suspected unsigned write

1. Freeze writes: scale the StatefulSet to 0 or revoke the orchestrator's
   agent in the identity fabric (its commits are then denied).
2. For each suspect CarePlan, fetch its Provenance and verify the signature
   against `/.well-known/jwks.json`; verify the audit chain for the workflow.
3. Engage the clinical safety officer to review affected patients.

## Replicas down

Check pod events, PVC binding (audit volumes) and node capacity. Workflows are
not lost: leases expire and surviving replicas take over.
