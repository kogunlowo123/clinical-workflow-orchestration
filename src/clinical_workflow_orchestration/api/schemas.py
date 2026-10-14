"""Request and response bodies for the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from clinical_workflow_orchestration.clinical.evidence import EvidencePacket
from clinical_workflow_orchestration.domain.careplan import (
    CarePlanDraft,
    CarePlanProposal,
    MedicationProposal,
    ModelProvenance,
    ValidationOutcome,
    ValidationReport,
)
from clinical_workflow_orchestration.domain.workflow import (
    Acknowledgement,
    CommitResult,
    Failure,
    PolicyRecord,
    SignOff,
    SignOffDecision,
    Transition,
    Workflow,
    WorkflowState,
)
from clinical_workflow_orchestration.fhir.resources import FhirId


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encounter_id: FhirId = Field(description="FHIR Encounter id the care plan follows from")


class DraftView(BaseModel):
    revision: int
    digest: str = Field(description="Send back as draft_digest when deciding")
    proposal: CarePlanProposal
    drafted_by: str
    drafted_at: datetime
    evidence_digest: str
    model: ModelProvenance

    @classmethod
    def of(cls, draft: CarePlanDraft) -> DraftView:
        return cls(digest=draft.digest, **draft.model_dump())


class ValidationView(BaseModel):
    outcome: ValidationOutcome
    report: ValidationReport

    @classmethod
    def of(cls, report: ValidationReport) -> ValidationView:
        return cls(outcome=report.outcome, report=report)


class WorkflowView(BaseModel):
    workflow_id: str
    state: WorkflowState
    encounter_id: str
    patient_id: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    revision: int
    revision_feedback: tuple[str, ...]
    evidence: EvidencePacket | None
    draft: DraftView | None
    validation: ValidationView | None
    approval: PolicyRecord | None
    signoff: SignOff | None
    commit: CommitResult | None
    failure: Failure | None
    history: tuple[Transition, ...]

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowView:
        return cls(
            workflow_id=workflow.id,
            state=workflow.state,
            encounter_id=workflow.encounter_id,
            patient_id=workflow.patient_id,
            created_by=workflow.created_by,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
            revision=workflow.revision,
            revision_feedback=workflow.revision_feedback,
            evidence=workflow.evidence,
            draft=DraftView.of(workflow.draft) if workflow.draft else None,
            validation=ValidationView.of(workflow.validation) if workflow.validation else None,
            approval=workflow.approval,
            signoff=workflow.signoff,
            commit=workflow.commit,
            failure=workflow.failure,
            history=workflow.history,
        )


class WorkflowListItem(BaseModel):
    workflow_id: str
    state: WorkflowState
    encounter_id: str
    patient_id: str | None
    revision: int
    validation_outcome: ValidationOutcome | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowListItem:
        return cls(
            workflow_id=workflow.id,
            state=workflow.state,
            encounter_id=workflow.encounter_id,
            patient_id=workflow.patient_id,
            revision=workflow.revision,
            validation_outcome=workflow.validation.outcome if workflow.validation else None,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )


class WorkflowList(BaseModel):
    items: list[WorkflowListItem]
    limit: int
    offset: int


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SignOffDecision
    draft_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    comment: str | None = Field(default=None, max_length=2000)
    acknowledgements: tuple[Acknowledgement, ...] = Field(default=(), max_length=100)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


class MedicationCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: FhirId
    proposals: list[MedicationProposal] = Field(min_length=1, max_length=25)


class OAuthError(BaseModel):
    error: str
    error_description: str
