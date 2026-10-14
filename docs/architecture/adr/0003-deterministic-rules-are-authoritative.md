# ADR-0003: Deterministic rules are authoritative; model review is advisory

- Status: Accepted
- Date: 2026-10-14

## Context

Model reviewers find problems rules cannot express, but they also produce
false positives and, occasionally, false reassurance. Allergy contraindication,
duplicate therapy, renal dosing and follow-up timing are well-defined checks
that must behave identically every time and be explainable to a pharmacist.

## Decision

- Deterministic checks run first, from versioned knowledge tables
  (`clinical/terminology.py`, `RULESET_VERSION`). Their errors can only be
  cleared by the signing clinician acknowledging each finding with a reason.
- Model review concerns are capped at WARNING: a model can neither block nor
  clear a plan, and activity references it returns are range-checked.
- If no model is available the deterministic report is still produced and the
  clinician sees `review_status=unavailable`.
- Grounding is a deterministic check: every resource a draft cites must exist
  in the evidence packet, which catches hallucinated references.

## Consequences

- Safety-relevant behaviour is testable and reproducible; the ruleset version
  is recorded on every report.
- Knowledge tables need clinical ownership and change control (CODEOWNERS on
  `src/clinical_workflow_orchestration/clinical/`).
- Local formularies extend the tables, not prompts.
