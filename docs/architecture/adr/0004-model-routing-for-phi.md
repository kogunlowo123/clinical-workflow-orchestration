# ADR-0004: PHI-safe model routing

- Status: Accepted
- Date: 2026-10-14

## Context

Drafting needs a capable model; the record is PHI. Provider outages must not
stop discharges, and failover must never route PHI to an unapproved provider.

## Decision

- Every model request is labelled `RESTRICTED`; the agentic-core ModelRouter
  only considers providers whose configured `max_classification` is 3.
- `configs/providers.yaml` lists Azure OpenAI (private endpoint, covered by the
  BAA) as primary and a vLLM-served model inside the cluster as fallback.
  Deployment values are `${VARIABLES}` that fail closed when unset.
- The model sees a de-identified view: age band (90+ collapsed), sex, codes,
  values, clinical event dates and resource ids; never names, addresses,
  contact details, birth dates, MRNs or patient ids. ContentGuard redacts
  residual identifiers and blocks injection and secrets.
- Record content is passed as EXTERNAL-trust data between random boundaries.
  Injection detected in the record fails the workflow with
  `security_violation` instead of drafting from manipulated input.

## Consequences

- An outage of the primary model degrades quality, not availability.
- Prompt injection in chart free text is contained, at the cost of occasional
  false positives that surface as a failed workflow for human handling.
- Spend is attributable per task and provider from configured contract prices.
