# ADR-0008: StatefulSet for per-replica audit chains

- Status: Accepted
- Date: 2026-10-14

## Context

The agentic-core audit log is a hash chain whose head must survive restarts so
the next event links to it. Workflow state is shared in PostgreSQL, but each
replica writes its own chain.

## Decision

Run the orchestrator as a StatefulSet with a small persistent volume per
replica for `/var/lib/clinical-workflow/audit/<pod>.jsonl`. A log shipper
copies chains to immutable Blob storage provisioned by Terraform. Workflows
are not bound to replicas: database leases decide which replica advances a
workflow.

## Consequences

- Chains are continuous per pod identity across restarts and verifiable with
  `verify_chain`.
- Scaling down leaves a chain volume for the removed ordinal; it is shipped and
  retained, never deleted, per the retention policy.
