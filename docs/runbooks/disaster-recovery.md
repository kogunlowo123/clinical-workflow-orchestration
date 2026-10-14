# Runbook: disaster recovery

| Component | RPO | RTO | Mechanism |
|---|---|---|---|
| Workflow state (PostgreSQL) | ≤ 5 min | 1 h | Zone-redundant HA; geo-redundant backups; point-in-time restore |
| Audit chains | 0 for shipped chains | 4 h | Immutable GZRS storage; per-replica volumes |
| Keys | 0 | 30 min | Key Vault soft delete and purge protection |
| Application | 0 | 30 min | Image and chart in GHCR; Terraform and Helm values in Git |

## Region loss

1. Restore PostgreSQL from geo-redundant backup into the paired region.
2. Apply Terraform for the paired region (Key Vault, storage, OpenAI) and
   restore Key Vault secrets from backup.
3. Install the chart with the regional values; update DNS.
4. Workflows resume: those that were mid-step are picked up by the sweep;
   commits recover by identifier search before writing (ADR-0006).

## Database corruption or bad migration

Restore to a point in time before the event into a new server, point the
`DATABASE_URL` secret at it, restart. Workflows that progressed after the
restore point are re-run from their restored state; EHR writes made in that
window are found by the commit recovery search and not duplicated.

## Verification after recovery

- `/readyz` on all replicas.
- Verify the latest audit chain files with `verify_chain`.
- Run the synthetic end-to-end check (startup runbook, step 5).
