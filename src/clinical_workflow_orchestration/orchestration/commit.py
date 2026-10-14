"""Commit step: write the signed care plan, tasks and Provenance to the EHR.

Preconditions are re-checked here, not trusted from earlier steps:

1. an approving sign-off with a valid platform signature exists;
2. the signature covers exactly the draft being written (digest match);
3. the governance policy allows the write *now* (policy may have changed);
4. when the identity fabric is configured, the fabric does not deny this
   service's own credential the write.

The commit is idempotent: if a previous attempt reached the EHR (crash
after the transaction, before the workflow was saved), the existing
resources are found by identifier and reused instead of written again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agentic_core.domain.classification import DataClassification, Decision

from clinical_workflow_orchestration.domain.errors import (
    ApprovalRequiredError,
    FhirError,
    ForbiddenError,
    InvalidStateError,
)
from clinical_workflow_orchestration.domain.workflow import CommitResult, SignOffDecision, Workflow
from clinical_workflow_orchestration.fhir.builders import (
    build_commit_bundle,
    bundle_digest,
    careplan_identifier,
)
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.fhir.resources import location_reference
from clinical_workflow_orchestration.governance.approval import EHR_WRITE, Governance
from clinical_workflow_orchestration.integrations.identity_fabric import (
    IdentityFabricClient,
    ServiceCredentials,
)
from clinical_workflow_orchestration.security.signing import PlatformKey


class WriteGate(Protocol):
    async def check(self, workflow: Workflow) -> None: ...


class FabricWriteGate:
    """Ask the identity fabric whether this service's credential may write the plan.

    ``require_approval`` from the fabric is satisfied by the clinician
    sign-off that precedes every commit; ``deny`` blocks the write.
    """

    def __init__(self, fabric: IdentityFabricClient, credentials: ServiceCredentials) -> None:
        self._fabric = fabric
        self._credentials = credentials

    async def check(self, workflow: Workflow) -> None:
        signer = workflow.signoff.practitioner if workflow.signoff else None
        decision = await self._fabric.authorize(
            await self._credentials.token(),
            action=EHR_WRITE,
            resource_type="CarePlan",
            resource_id=workflow.id,
            classification=DataClassification.RESTRICTED,
            environment={"purpose_of_use": "TREAT", "clinician_signed": True, "signer": signer},
        )
        if decision.decision is Decision.DENY:
            raise ForbiddenError(f"identity fabric denied the EHR write: {decision.reason}")


class CarePlanCommitter:
    def __init__(
        self,
        fhir: FhirClient,
        governance: Governance,
        signer: PlatformKey,
        *,
        identifier_system: str,
        agent_id: str,
        orchestrator_id: str,
        write_gate: WriteGate | None = None,
    ) -> None:
        self._fhir = fhir
        self._governance = governance
        self._signer = signer
        self._system = identifier_system
        self._agent_id = agent_id
        self._orchestrator_id = orchestrator_id
        self._gate = write_gate

    async def commit(self, workflow: Workflow, *, at: datetime) -> CommitResult:
        signoff, draft, evidence = workflow.signoff, workflow.draft, workflow.evidence
        if signoff is None or signoff.decision is not SignOffDecision.APPROVE:
            raise InvalidStateError("commit requires an approving clinician sign-off")
        if draft is None or evidence is None:
            raise InvalidStateError("commit requires a draft and its evidence")
        if signoff.signature is None:
            raise InvalidStateError("sign-off is not signed")
        claims = self._signer.verify_jws(signoff.signature)
        if claims.get("draft_digest") != draft.digest or signoff.draft_digest != draft.digest:
            raise InvalidStateError("the signed plan differs from the plan being committed")
        if claims.get("wf") != workflow.id or claims.get("practitioner") != signoff.practitioner:
            raise InvalidStateError("sign-off signature does not match this workflow")

        decision = self._governance.ehr_write(workflow, at=at)
        if decision.decision is Decision.DENY:
            raise ForbiddenError(f"policy denied the EHR write: {decision.reason}")
        if decision.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(f"policy still requires approval: {decision.reason}")
        if self._gate is not None:
            await self._gate.check(workflow)

        recovered = await self._recover(workflow, at=at)
        if recovered is not None:
            return recovered
        bundle = build_commit_bundle(
            workflow_id=workflow.id,
            packet=evidence,
            draft=draft,
            signoff=signoff,
            identifier_system=self._system,
            agent_id=self._agent_id,
            orchestrator_id=self._orchestrator_id,
        )
        response = await self._fhir.transaction(bundle)
        locations: list[str] = []
        for entry in response.get("entry") or []:
            location = (
                (entry.get("response") or {}).get("location") if isinstance(entry, dict) else None
            )
            if not isinstance(location, str):
                raise FhirError("transaction-response entry without a location", retryable=False)
            locations.append(location_reference(location))
        expected = len(bundle["entry"])
        if len(locations) != expected:
            raise FhirError(
                f"transaction-response has {len(locations)} entries, expected {expected}",
                retryable=False,
            )
        return CommitResult(
            care_plan=locations[0],
            tasks=tuple(locations[1:-1]),
            provenance=locations[-1],
            committed_at=at,
            bundle_digest=bundle_digest(bundle),
        )

    async def _recover(self, workflow: Workflow, *, at: datetime) -> CommitResult | None:
        plans = await self._fhir.search(
            "CarePlan", {"identifier": f"{self._system}|{careplan_identifier(workflow.id)}"}
        )
        if not plans:
            return None
        care_plan = f"CarePlan/{plans[0]['id']}"
        tasks = await self._fhir.search("Task", {"based-on": care_plan})
        provenance = await self._fhir.search("Provenance", {"target": care_plan})
        if not provenance:
            raise FhirError(
                f"{care_plan} exists without Provenance; manual reconciliation required",
                retryable=False,
            )
        return CommitResult(
            care_plan=care_plan,
            tasks=tuple(sorted(f"Task/{t['id']}" for t in tasks)),
            provenance=f"Provenance/{provenance[0]['id']}",
            committed_at=at,
            bundle_digest="recovered",
        )
