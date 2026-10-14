# Runbook: monitoring

Dashboard: `configs/grafana/clinical-workflow-orchestration.json`. Alerts:
`configs/prometheus/alerts.yaml`.

| Signal | Healthy | Investigate when |
|---|---|---|
| `cwo:request_errors:ratio_5m` | < 0.1% | Fast burn alert fires |
| Step duration p95 (`cwo_workflow_step_duration_seconds`) | retrieval < 5 s, drafting < 60 s, validating < 60 s, commit < 5 s | Drafting p95 > 120 s |
| `cwo_workflow_failures_total` by code | Rare `fhir_error`, `provider_unavailable` | Any `security_violation`; sustained other codes |
| `cwo_validation_findings_total` | Stable mix | Sudden jump in a check: drafting quality regression or record changes |
| `cwo_signoffs_total` | Approvals dominate | Rising `reject` or `request_changes`: draft quality issue |
| `cwo_model_attempts_total` | Primary `success` | Primary failures: failover in effect |
| `cwo_model_cost_usd_total` | Within budget | Spend alert |
| `cwo_inbound_agent_decisions_total` | Mostly `allow` | Deny ratio > 20% |

## Latency

1. Check which route is slow on the dashboard (route label is the template,
   not the concrete path).
2. Workflow reads hit only PostgreSQL: check database CPU and connections.
3. Step latency is dominated by the EHR and models; compare
   `cwo_fhir_requests_total` outcomes and model attempt outcomes.

## Model latency

Drafting time rises when the primary is throttled and the fallback serves
traffic. Check Azure OpenAI TPM utilization and the vLLM deployment's queue;
raise TPM quota or vLLM replicas.

## Cost

Spend per completed workflow =
`increase(cwo_model_cost_usd_total[1d]) / increase(cwo_workflow_transitions_total{to_state="completed"}[1d])`.
A rise without more workflows means more revisions or longer records.

## Worklist health

Workflows waiting for sign-off: `GET /v1/workflows?state=awaiting_approval`.
Failed workflows needing a person: `GET /v1/workflows?state=failed` (those
with `next_retry_at = null`).
