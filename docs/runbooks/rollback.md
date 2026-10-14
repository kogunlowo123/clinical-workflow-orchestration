# Runbook: rollback

The schema is additive within a minor version (`schema_version` table) and the
workflow document tolerates unknown fields being absent, so rolling the
application back within 0.x.y needs no database change.

```bash
helm -n clinical-workflow history cwo
helm -n clinical-workflow rollback cwo <revision> --wait
kubectl -n clinical-workflow rollout status statefulset/cwo-clinical-workflow-orchestration
curl -sf "$CWO_URL/readyz"
```

In-flight workflows continue on the rolled-back version: their state is in
PostgreSQL and steps are idempotent.

Policy-only regressions: revert the commit that touched
`policies/clinical-workflow.yaml`, release, and confirm the `policy_digest`
recorded on new workflow approvals matches the reverted file.

Knowledge-table regressions (`clinical/terminology.py`): revert and release;
validation reports carry `ruleset_version`, so affected workflows can be found
and revalidated by requesting changes.

Every release image and chart stays in GHCR, so any previous version can be
reinstalled.
