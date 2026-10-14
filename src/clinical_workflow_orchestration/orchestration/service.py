"""Clinical workflow use cases: start, inspect, decide, resume, cancel, medication check.

Callers are authorized before they reach this service (REST: enterprise
OIDC roles; A2A/MCP: agent credentials). Clinician decisions are
additionally decided by governance policy here because their outcome
depends on the workflow's own content (unacknowledged findings, drafter).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision
from agentic_core.domain.identifiers import new_id
from agentic_core.observability import correlation_id

from clinical_workflow_orchestration.clinical.evidence import EvidenceRetriever
from clinical_workflow_orchestration.clinical.rules import validate_medications
from clinical_workflow_orchestration.domain.careplan import MedicationProposal, ValidationFinding
from clinical_workflow_orchestration.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    OrchestrationError,
)
from clinical_workflow_orchestration.domain.ports import Clock, WorkflowRepository
from clinical_workflow_orchestration.domain.workflow import (
    TERMINAL,
    Acknowledgement,
    SignOff,
    SignOffDecision,
    Workflow,
    WorkflowState,
)
from clinical_workflow_orchestration.governance.approval import Governance
from clinical_workflow_orchestration.observability.metrics import OrchestrationMetrics
from clinical_workflow_orchestration.orchestration.engine import WorkflowEngine
from clinical_workflow_orchestration.security.signing import PlatformKey
from clinical_workflow_orchestration.security.users import UserPrincipal

SIGNOFF_ACTIONS = {
    SignOffDecision.APPROVE: "careplan:approve",
    SignOffDecision.REJECT: "careplan:reject",
    SignOffDecision.REQUEST_CHANGES: "careplan:request-changes",
}

Scheduler = Callable[[str], None]


class ClinicalWorkflowService:
    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        engine: WorkflowEngine,
        governance: Governance,
        retriever: EvidenceRetriever,
        signer: PlatformKey,
        audit: AuditLog,
        metrics: OrchestrationMetrics,
        clock: Clock,
        max_revisions: int = 3,
    ) -> None:
        self._repo = repository
        self._engine = engine
        self._governance = governance
        self._retriever = retriever
        self._signer = signer
        self._audit = audit
        self._metrics = metrics
        self._clock = clock
        self._max_revisions = max_revisions
        self._schedule: Scheduler = lambda _workflow_id: None

    def set_scheduler(self, schedule: Scheduler) -> None:
        self._schedule = schedule

    @property
    def governance(self) -> Governance:
        return self._governance

    async def start(
        self, *, encounter_id: str, idempotency_key: str | None, actor: str
    ) -> tuple[Workflow, bool]:
        now = self._clock.now()
        key = idempotency_key or f"encounter:{encounter_id}:{new_id()}"
        candidate = Workflow(
            id=new_id(),
            idempotency_key=key,
            encounter_id=encounter_id,
            state=WorkflowState.PENDING,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )
        stored = await self._repo.add(candidate)
        created = stored.id == candidate.id
        if created:
            await self._audit.record(
                actor=actor,
                action="workflow.start",
                resource=f"Workflow/{stored.id}",
                outcome="success",
                correlation_id=correlation_id(),
                details={"encounter": f"Encounter/{encounter_id}"},
            )
        elif stored.encounter_id != encounter_id:
            raise ConflictError("idempotency key was already used for a different encounter")
        self._schedule(stored.id)
        return stored, created

    async def get(self, workflow_id: str) -> Workflow:
        workflow = await self._repo.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"workflow {workflow_id} not found")
        return workflow

    async def list_workflows(
        self, *, state: WorkflowState | None, limit: int, offset: int
    ) -> Sequence[Workflow]:
        return await self._repo.list(state=state, limit=limit, offset=offset)

    async def decide(
        self,
        workflow_id: str,
        *,
        principal: UserPrincipal,
        decision: SignOffDecision,
        draft_digest: str,
        comment: str | None,
        acknowledgements: tuple[Acknowledgement, ...],
    ) -> Workflow:
        workflow = await self.get(workflow_id)
        if workflow.state is not WorkflowState.AWAITING_APPROVAL or workflow.draft is None:
            raise InvalidStateError(f"workflow is {workflow.state.value}, not awaiting approval")
        if draft_digest != workflow.draft.digest:
            raise ConflictError("the plan changed since it was displayed; reload and review again")
        known = {f.id for f in workflow.validation.findings} if workflow.validation else set()
        unknown = sorted(a.finding_id for a in acknowledgements if a.finding_id not in known)
        if unknown:
            raise OrchestrationError(f"acknowledgements reference unknown findings: {unknown}")
        if decision is SignOffDecision.REQUEST_CHANGES and not comment:
            raise OrchestrationError("requesting changes needs a comment describing them")

        now = self._clock.now()
        action = SIGNOFF_ACTIONS[decision]
        policy = self._governance.signoff(
            principal, workflow, action=action, acknowledgements=acknowledgements, at=now
        )
        self._metrics.policy_decisions.labels(action, policy.decision.value).inc()
        if policy.decision is not Decision.ALLOW:
            await self._audit.record(
                actor=principal.subject,
                action=f"workflow.signoff.{decision.value}",
                resource=f"Workflow/{workflow.id}",
                outcome="denied",
                correlation_id=correlation_id(),
                details={"rules": list(policy.matched_rules), "reason": policy.reason},
            )
            raise ForbiddenError(
                f"{action} denied by policy: {policy.reason} {policy.matched_rules}"
            )
        practitioner = principal.fhir_user or ""
        signoff = SignOff(
            decision=decision,
            practitioner=practitioner,
            subject=principal.subject,
            signed_at=now,
            draft_digest=draft_digest,
            comment=comment,
            acknowledgements=acknowledgements,
            token_id=principal.token_id,
        )
        if decision is SignOffDecision.APPROVE:
            signature = self._signer.sign_jws(
                {
                    "wf": workflow.id,
                    "draft_digest": draft_digest,
                    "practitioner": practitioner,
                    "sub": principal.subject,
                    "decision": decision.value,
                    "acknowledged": sorted(a.finding_id for a in acknowledgements),
                    "iat": int(now.timestamp()),
                    **({"jti": principal.token_id} if principal.token_id else {}),
                }
            )
            signoff = signoff.model_copy(
                update={"signature": signature, "signature_key_id": self._signer.key_id}
            )
            updated = workflow.transition(
                WorkflowState.COMMITTING,
                at=now,
                actor=principal.subject,
                reason=f"approved by {practitioner}",
                signoff=signoff,
            )
        elif decision is SignOffDecision.REJECT:
            updated = workflow.transition(
                WorkflowState.REJECTED,
                at=now,
                actor=principal.subject,
                reason=f"rejected by {practitioner}",
                signoff=signoff,
            )
        else:
            if workflow.revision >= self._max_revisions:
                raise InvalidStateError(
                    f"revision limit ({self._max_revisions}) reached; reject the plan instead"
                )
            updated = workflow.transition(
                WorkflowState.DRAFTING,
                at=now,
                actor=principal.subject,
                reason=f"changes requested by {practitioner}",
                revision=workflow.revision + 1,
                revision_feedback=(*workflow.revision_feedback, comment or ""),
                signoff=None,
            )
        saved = await self._repo.save(updated)
        self._metrics.signoffs.labels(decision.value).inc()
        await self._audit.record(
            actor=principal.subject,
            action=f"workflow.signoff.{decision.value}",
            resource=f"Workflow/{workflow.id}",
            outcome="success",
            correlation_id=correlation_id(),
            details={
                "practitioner": practitioner,
                "draft_digest": draft_digest,
                "acknowledged": sorted(a.finding_id for a in acknowledgements),
                "policy_digest": policy.policy_digest,
                "rules": list(policy.matched_rules),
            },
        )
        await self._engine.record_transition(workflow, saved, principal.subject)
        if saved.state in {WorkflowState.COMMITTING, WorkflowState.DRAFTING}:
            self._schedule(saved.id)
        return saved

    async def resume(self, workflow_id: str, *, actor: str) -> Workflow:
        workflow = await self.get(workflow_id)
        if workflow.state is not WorkflowState.FAILED or workflow.failure is None:
            raise InvalidStateError(f"workflow is {workflow.state.value}, not failed")
        resumed = workflow.transition(
            workflow.failure.step,
            at=self._clock.now(),
            actor=actor,
            reason=f"resumed after {workflow.failure.code}",
        )
        saved = await self._repo.save(resumed)
        await self._engine.record_transition(workflow, saved, actor)
        self._schedule(saved.id)
        return saved

    async def cancel(self, workflow_id: str, *, actor: str, reason: str) -> Workflow:
        workflow = await self.get(workflow_id)
        if workflow.state in TERMINAL or workflow.state is WorkflowState.COMMITTING:
            raise InvalidStateError(f"workflow is {workflow.state.value} and cannot be cancelled")
        cancelled = workflow.transition(
            WorkflowState.CANCELLED, at=self._clock.now(), actor=actor, reason=reason
        )
        saved = await self._repo.save(cancelled)
        await self._engine.record_transition(workflow, saved, actor)
        return saved

    async def check_medications(
        self, *, patient_id: str, proposals: Sequence[MedicationProposal], actor: str
    ) -> list[ValidationFinding]:
        packet = await self._retriever.for_patient(patient_id, now=self._clock.now())
        findings = validate_medications(packet, proposals)
        await self._audit.record(
            actor=actor,
            action="medication.check",
            resource=f"Patient/{patient_id}",
            outcome="success",
            correlation_id=correlation_id(),
            details={
                "proposals": len(proposals),
                "errors": sum(1 for f in findings if f.severity.value == "error"),
                "evidence_digest": packet.digest,
            },
        )
        return findings
