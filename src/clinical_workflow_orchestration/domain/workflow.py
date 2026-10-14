"""Clinical workflow aggregate and its state machine.

::

    pending -> retrieving_evidence -> drafting -> validating -> awaiting_approval
            -> committing -> completed

    awaiting_approval -> drafting      (clinician requests changes; bounded revisions)
    awaiting_approval -> rejected      (clinician rejects)
    <working state>   -> failed        (step error; resumable from the failed step)
    failed            -> <failed step> (resume, manual or automatic for transient errors)
    <non-terminal>    -> cancelled

Every transition is validated against :data:`TRANSITIONS` and appended to
the workflow history; the repository persists the aggregate with optimistic
concurrency so two replicas can never both advance the same version.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from agentic_core.domain.classification import Decision
from pydantic import BaseModel, ConfigDict, Field

from clinical_workflow_orchestration.clinical.evidence import EvidencePacket
from clinical_workflow_orchestration.domain.careplan import CarePlanDraft, ValidationReport
from clinical_workflow_orchestration.domain.errors import InvalidStateError


class WorkflowState(StrEnum):
    PENDING = "pending"
    RETRIEVING_EVIDENCE = "retrieving_evidence"
    DRAFTING = "drafting"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    COMMITTING = "committing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({WorkflowState.COMPLETED, WorkflowState.REJECTED, WorkflowState.CANCELLED})
# States in which the engine has work to do without waiting for a human.
RUNNABLE = frozenset(
    {
        WorkflowState.PENDING,
        WorkflowState.RETRIEVING_EVIDENCE,
        WorkflowState.DRAFTING,
        WorkflowState.VALIDATING,
        WorkflowState.COMMITTING,
    }
)
RESUMABLE_STEPS = frozenset(
    {
        WorkflowState.RETRIEVING_EVIDENCE,
        WorkflowState.DRAFTING,
        WorkflowState.VALIDATING,
        WorkflowState.COMMITTING,
    }
)

TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.PENDING: frozenset(
        {WorkflowState.RETRIEVING_EVIDENCE, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.RETRIEVING_EVIDENCE: frozenset(
        {WorkflowState.DRAFTING, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.DRAFTING: frozenset(
        {WorkflowState.VALIDATING, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.VALIDATING: frozenset(
        {WorkflowState.AWAITING_APPROVAL, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.AWAITING_APPROVAL: frozenset(
        {
            WorkflowState.COMMITTING,
            WorkflowState.DRAFTING,
            WorkflowState.REJECTED,
            WorkflowState.CANCELLED,
        }
    ),
    # A commit that has started can only finish or fail: cancelling mid-write
    # could leave partially acknowledged FHIR state without provenance.
    WorkflowState.COMMITTING: frozenset({WorkflowState.COMPLETED, WorkflowState.FAILED}),
    WorkflowState.FAILED: RESUMABLE_STEPS | {WorkflowState.CANCELLED},
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.REJECTED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}


class Transition(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_state: WorkflowState
    to_state: WorkflowState
    at: datetime
    actor: str
    reason: str


class PolicyRecord(BaseModel):
    """A policy decision as recorded on the workflow (which policy, which rules, why)."""

    model_config = ConfigDict(frozen=True)

    action: str
    decision: Decision
    matched_rules: tuple[str, ...]
    policy_name: str
    policy_version: str
    policy_digest: str
    reason: str
    decided_at: datetime


class Acknowledgement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=3, max_length=300)
    reason: str = Field(min_length=10, max_length=1000)


class SignOffDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class SignOff(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: SignOffDecision
    practitioner: str = Field(description="FHIR reference of the signer, e.g. Practitioner/123")
    subject: str = Field(description="Identity-provider subject of the signer")
    signed_at: datetime
    draft_digest: str
    comment: str | None = None
    acknowledgements: tuple[Acknowledgement, ...] = ()
    token_id: str | None = Field(default=None, description="jti of the signer's access token")
    signature: str | None = Field(default=None, description="Compact JWS over the sign-off")
    signature_key_id: str | None = None


class CommitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    care_plan: str
    tasks: tuple[str, ...]
    provenance: str
    committed_at: datetime
    bundle_digest: str


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: WorkflowState
    code: str
    message: str
    retryable: bool
    attempts: int = Field(ge=1)
    failed_at: datetime
    next_retry_at: datetime | None = None


class Workflow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    idempotency_key: str
    encounter_id: str
    patient_id: str | None = None
    state: WorkflowState
    version: int = Field(default=0, ge=0)
    created_by: str
    created_at: datetime
    updated_at: datetime
    revision: int = 0
    revision_feedback: tuple[str, ...] = ()
    evidence: EvidencePacket | None = None
    draft: CarePlanDraft | None = None
    validation: ValidationReport | None = None
    approval: PolicyRecord | None = None
    signoff: SignOff | None = None
    commit: CommitResult | None = None
    failure: Failure | None = None
    history: tuple[Transition, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def transition(
        self, to: WorkflowState, *, at: datetime, actor: str, reason: str, **changes: object
    ) -> Workflow:
        if to not in TRANSITIONS[self.state]:
            raise InvalidStateError(f"workflow {self.id} cannot move from {self.state} to {to}")
        entry = Transition(from_state=self.state, to_state=to, at=at, actor=actor, reason=reason)
        update: dict[str, object] = {
            "state": to,
            "updated_at": at,
            "history": (*self.history, entry),
            **changes,
        }
        # A resumed step keeps its failure record so repeated failures accumulate
        # attempts (bounding automatic retries); completing the step clears it.
        if "failure" not in changes and self.state is not WorkflowState.FAILED:
            update["failure"] = None
        return self.model_copy(update=update)
