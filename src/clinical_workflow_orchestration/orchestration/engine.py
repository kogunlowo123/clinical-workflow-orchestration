"""Workflow engine: advances persisted workflows one idempotent step at a time.

Execution model:

* A replica must hold the workflow's lease to advance it; leases expire, so
  a crashed replica's work is picked up by another (crash recovery).
* Each step's output and the state transition are saved in one optimistic
  update; if anything else changed the workflow meanwhile (a cancel), the
  step's result is discarded.
* A step that fails moves the workflow to ``failed`` with a structured
  failure; retryable failures (FHIR or model outage) are retried
  automatically with exponential backoff up to a bound, then wait for a
  person to resume or cancel.
* The engine stops at ``awaiting_approval``: only a clinician decision moves
  a workflow past it.
"""

from __future__ import annotations

import time
from datetime import timedelta

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision
from agentic_core.domain.errors import (
    AgenticCoreError,
    PolicyDeniedError,
    ProviderUnavailableError,
    SecurityViolationError,
)
from agentic_core.observability import correlation_id, get_logger

from clinical_workflow_orchestration.agents.care_coordination import CareCoordinationAgent
from clinical_workflow_orchestration.agents.clinical_validation import ClinicalValidationAgent
from clinical_workflow_orchestration.clinical.evidence import EvidenceRetriever
from clinical_workflow_orchestration.domain.errors import (
    ConflictError,
    InvalidStateError,
    OrchestrationError,
)
from clinical_workflow_orchestration.domain.ports import Clock, WorkflowRepository
from clinical_workflow_orchestration.domain.workflow import (
    RUNNABLE,
    Failure,
    Workflow,
    WorkflowState,
)
from clinical_workflow_orchestration.governance.approval import Governance
from clinical_workflow_orchestration.observability.metrics import OrchestrationMetrics
from clinical_workflow_orchestration.orchestration.commit import CarePlanCommitter

log = get_logger("clinical_workflow_orchestration.engine")

ENGINE_ACTOR = "clinical-workflow-engine"


def classify_failure(exc: Exception) -> tuple[str, str, bool]:
    """(code, safe message, retryable) for a step exception."""
    if isinstance(exc, OrchestrationError):
        return exc.code, exc.message, exc.retryable
    if isinstance(exc, ProviderUnavailableError):
        return exc.code, str(exc), True
    if isinstance(exc, SecurityViolationError | PolicyDeniedError):
        return exc.code, str(exc), False
    if isinstance(exc, AgenticCoreError):
        return exc.code, str(exc), False
    # Unknown errors: do not echo the message (it may contain record content).
    return "internal_error", f"unexpected {type(exc).__name__}", False


class WorkflowEngine:
    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        retriever: EvidenceRetriever,
        coordinator: CareCoordinationAgent,
        validator: ClinicalValidationAgent,
        governance: Governance,
        committer: CarePlanCommitter,
        audit: AuditLog,
        metrics: OrchestrationMetrics,
        clock: Clock,
        owner: str,
        lease_seconds: int = 300,
        max_auto_retries: int = 3,
        retry_base_seconds: int = 30,
    ) -> None:
        self._repo = repository
        self._retriever = retriever
        self._coordinator = coordinator
        self._validator = validator
        self._governance = governance
        self._committer = committer
        self._audit = audit
        self._metrics = metrics
        self._clock = clock
        self._owner = owner
        self._lease = timedelta(seconds=lease_seconds)
        self._max_auto_retries = max_auto_retries
        self._retry_base = retry_base_seconds

    async def record_transition(self, before: Workflow, after: Workflow, actor: str) -> None:
        self._metrics.transitions.labels(before.state.value, after.state.value).inc()
        last = after.history[-1] if after.history else None
        await self._audit.record(
            actor=actor,
            action=f"workflow.{after.state.value}",
            resource=f"Workflow/{after.id}",
            outcome="success" if after.state is not WorkflowState.FAILED else "failure",
            correlation_id=correlation_id(),
            details={
                "from": before.state.value,
                "to": after.state.value,
                "reason": last.reason if last else "",
                "version": after.version,
                "encounter": f"Encounter/{after.encounter_id}",
                **({"failure": after.failure.code} if after.failure else {}),
            },
        )

    async def advance(self, workflow_id: str) -> Workflow | None:
        """Run steps until the workflow waits for a person, finishes or fails."""
        now = self._clock.now()
        if not await self._repo.acquire_lease(
            workflow_id, owner=self._owner, now=now, until=now + self._lease
        ):
            return None
        try:
            while True:
                workflow = await self._repo.get(workflow_id)
                if workflow is None:
                    return None
                if workflow.state is WorkflowState.FAILED:
                    workflow = await self._auto_resume(workflow)
                    if workflow is None or workflow.state is WorkflowState.FAILED:
                        return workflow
                if workflow.state not in RUNNABLE:
                    return workflow
                now = self._clock.now()
                await self._repo.acquire_lease(
                    workflow_id, owner=self._owner, now=now, until=now + self._lease
                )
                try:
                    workflow = await self._step(workflow)
                except ConflictError:
                    log.info("step_result_discarded", workflow=workflow_id)
                    return await self._repo.get(workflow_id)
                if workflow.state is WorkflowState.FAILED:
                    return workflow
        finally:
            await self._repo.release_lease(workflow_id, owner=self._owner)

    async def _auto_resume(self, workflow: Workflow) -> Workflow | None:
        failure = workflow.failure
        now = self._clock.now()
        if (
            failure is None
            or not failure.retryable
            or failure.next_retry_at is None
            or failure.next_retry_at > now
        ):
            return workflow
        resumed = workflow.transition(
            failure.step,
            at=now,
            actor=ENGINE_ACTOR,
            reason=f"automatic retry {failure.attempts} after {failure.code}",
        )
        try:
            saved = await self._repo.save(resumed)
        except ConflictError:
            return None
        await self.record_transition(workflow, saved, ENGINE_ACTOR)
        return saved

    async def _step(self, workflow: Workflow) -> Workflow:
        started = time.perf_counter()
        step = workflow.state
        try:
            updated = await self._run(workflow)
        except ConflictError:
            raise
        except Exception as exc:
            self._metrics.step_duration.labels(step.value, "failure").observe(
                time.perf_counter() - started
            )
            return await self._fail(workflow, exc)
        saved = await self._repo.save(updated)
        self._metrics.step_duration.labels(step.value, "success").observe(
            time.perf_counter() - started
        )
        await self.record_transition(workflow, saved, ENGINE_ACTOR)
        return saved

    async def _run(self, workflow: Workflow) -> Workflow:
        now = self._clock.now()
        match workflow.state:
            case WorkflowState.PENDING:
                return workflow.transition(
                    WorkflowState.RETRIEVING_EVIDENCE, at=now, actor=ENGINE_ACTOR, reason="started"
                )
            case WorkflowState.RETRIEVING_EVIDENCE:
                packet = await self._retriever.for_encounter(workflow.encounter_id, now=now)
                return workflow.transition(
                    WorkflowState.DRAFTING,
                    at=self._clock.now(),
                    actor=self._retriever_actor(),
                    reason=f"evidence retrieved from {len(packet.sources)} resources",
                    evidence=packet,
                    patient_id=packet.patient_id,
                )
            case WorkflowState.DRAFTING:
                evidence = _require(workflow.evidence, "evidence")
                draft = await self._coordinator.draft(
                    evidence,
                    revision=workflow.revision,
                    feedback=workflow.revision_feedback,
                    now=now,
                )
                return workflow.transition(
                    WorkflowState.VALIDATING,
                    at=self._clock.now(),
                    actor=self._coordinator.agent_id,
                    reason=f"draft revision {draft.revision} with "
                    f"{len(draft.proposal.activities)} activities",
                    draft=draft,
                    validation=None,
                    approval=None,
                    signoff=None,
                )
            case WorkflowState.VALIDATING:
                return await self._validate(workflow)
            case WorkflowState.COMMITTING:
                result = await self._committer.commit(workflow, at=now)
                return workflow.transition(
                    WorkflowState.COMPLETED,
                    at=self._clock.now(),
                    actor=ENGINE_ACTOR,
                    reason=f"written {result.care_plan} with {len(result.tasks)} tasks",
                    commit=result,
                )
            case _:  # pragma: no cover - guarded by RUNNABLE
                raise ConflictError(f"state {workflow.state} is not runnable")

    async def _validate(self, workflow: Workflow) -> Workflow:
        evidence = _require(workflow.evidence, "evidence")
        draft = _require(workflow.draft, "draft")
        report = await self._validator.validate(evidence, draft, now=self._clock.now())
        for finding in report.findings:
            self._metrics.findings.labels(finding.check.split(":")[0], finding.severity.value).inc()
        candidate = workflow.model_copy(update={"validation": report})
        decision = self._governance.ehr_write(candidate, at=self._clock.now())
        self._metrics.policy_decisions.labels(decision.action, decision.decision.value).inc()
        if decision.decision is Decision.DENY:
            raise PolicyDeniedError(
                f"policy denied presenting the plan for approval: {decision.reason}",
                rule_ids=list(decision.matched_rules),
            )
        # Even if a misconfigured policy allowed an unsigned write, the engine
        # stops here: the commit step requires a signed clinician approval.
        return workflow.transition(
            WorkflowState.AWAITING_APPROVAL,
            at=self._clock.now(),
            actor=self._validator.agent_id,
            reason=f"validation {report.outcome.value}: {len(report.errors)} errors, "
            f"{len(report.warnings)} warnings",
            validation=report,
            approval=decision,
        )

    async def _fail(self, workflow: Workflow, exc: Exception) -> Workflow:
        code, message, retryable = classify_failure(exc)
        self._metrics.failures.labels(workflow.state.value, code).inc()
        now = self._clock.now()
        previous = workflow.failure
        attempts = previous.attempts + 1 if previous and previous.step is workflow.state else 1
        next_retry = None
        if retryable and attempts <= self._max_auto_retries:
            next_retry = now + timedelta(seconds=self._retry_base * 2 ** (attempts - 1))
        failure = Failure(
            step=workflow.state,
            code=code,
            message=message[:1000],
            retryable=retryable,
            attempts=attempts,
            failed_at=now,
            next_retry_at=next_retry,
        )
        log.warning(
            "workflow_step_failed",
            workflow=workflow.id,
            step=workflow.state.value,
            code=code,
            retryable=retryable,
            attempts=attempts,
        )
        failed = workflow.transition(
            WorkflowState.FAILED,
            at=now,
            actor=ENGINE_ACTOR,
            reason=f"{workflow.state.value} failed: {code}",
            failure=failure,
        )
        saved = await self._repo.save(failed)
        await self.record_transition(workflow, saved, ENGINE_ACTOR)
        return saved

    def _retriever_actor(self) -> str:
        return "evidence-retriever"


def _require[T](value: T | None, name: str) -> T:
    if value is None:
        raise InvalidStateError(f"workflow is missing its {name}")
    return value
