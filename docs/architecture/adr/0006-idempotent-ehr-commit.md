# ADR-0006: Idempotent EHR commit

- Status: Accepted
- Date: 2026-10-14

## Context

The commit writes CarePlan, Tasks and Provenance in one FHIR transaction. A
gateway timeout after the EHR committed leaves us not knowing whether the write
happened. Retrying blindly duplicates resources, and R4 Provenance has no
identifier element for conditional create.

## Decision

- CarePlan and Tasks carry workflow-scoped identifiers and use `ifNoneExist`
  conditional create; full URLs are deterministic `urn:uuid` values.
- The FHIR client does not retry transaction POSTs at the HTTP layer.
- Before writing, the commit step searches for the CarePlan by identifier. If
  found, it recovers Task references (`based-on`) and the Provenance
  (`target`) and completes without writing. A CarePlan without Provenance is
  surfaced for manual reconciliation instead of being patched silently.
- Transient commit failures fail the workflow as retryable; the engine retries
  with backoff, starting with the recovery search.

## Consequences

- A lost response costs one extra search, never a duplicate (chaos test
  `test_lost_commit_response_is_recovered_without_duplicates`).
- The EHR must support conditional create and the standard R4 search
  parameters CarePlan `identifier`, Task `based-on` and Provenance `target`.
