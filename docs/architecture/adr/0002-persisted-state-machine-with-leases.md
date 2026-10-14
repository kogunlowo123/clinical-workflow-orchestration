# ADR-0002: Persisted state machine with leases instead of a workflow engine product

- Status: Accepted
- Date: 2026-10-14

## Context

Workflows wait on people for hours and on EHRs and models for seconds to
minutes; replicas restart. We need resumability, exactly-once effects on the
EHR and an auditable history, without operating Temporal, Durable Functions or
Step Functions next to a cloud-portable service.

## Decision

- The workflow is one aggregate stored as a JSON document in PostgreSQL with
  promoted columns: `state`, `version`, `lease_owner`, `lease_expires_at`,
  `next_attempt_at`.
- Transitions are validated against an explicit table and appended to the
  workflow history.
- Each step's output and its transition are written in a single optimistic
  update (`WHERE version = :expected`). A concurrent change, such as a cancel,
  makes the step's save fail and its result is discarded.
- A replica must hold the lease to advance a workflow. Leases expire; a sweep
  picks up workflows with expired leases and retryable failures whose backoff
  has elapsed. Automatic retries are bounded; then a person resumes or cancels.
- Steps are idempotent: re-running retrieval, drafting or validation only
  replaces outputs; the EHR commit is idempotent by design (ADR-0006).

## Consequences

- No extra platform to run; the same code runs on Azure, AWS and GCP managed
  PostgreSQL.
- Throughput is bounded by database writes per transition, far above the
  clinical volume of a hospital.
- Long human waits cost nothing: a waiting workflow holds no lease.
