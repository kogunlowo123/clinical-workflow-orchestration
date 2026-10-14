"""Care-plan proposal produced by the care-coordination agent, and its validation report."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from agentic_core.domain.provenance import canonical_json, content_digest
from pydantic import BaseModel, ConfigDict, Field

from clinical_workflow_orchestration.clinical.terminology import Severity


class ActivityKind(StrEnum):
    FOLLOW_UP = "follow-up"  # clinic / telehealth visit
    LAB = "lab"  # laboratory test or recheck
    REFERRAL = "referral"
    MEDICATION = "medication"  # start / stop / continue / adjust for clinician review
    EDUCATION = "education"
    MONITORING = "monitoring"  # home monitoring, e.g. daily weights


class Priority(StrEnum):
    ROUTINE = "routine"
    URGENT = "urgent"
    ASAP = "asap"
    STAT = "stat"


class MedicationAction(StrEnum):
    START = "start"
    STOP = "stop"
    CONTINUE = "continue"
    ADJUST = "adjust"


class MedicationProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: MedicationAction
    display: str = Field(min_length=2, max_length=200, description="Drug, strength and form")
    rxnorm_code: str | None = Field(default=None, pattern=r"^[0-9]{1,10}$")
    existing_request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9\-.]{1,64}$",
        description="Required for stop/continue/adjust: the active MedicationRequest id",
    )


class ProposedActivity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ActivityKind
    description: str = Field(min_length=3, max_length=500)
    code: str | None = Field(
        default=None, max_length=32, description="LOINC code for labs, SNOMED for others"
    )
    due_in_days: int = Field(ge=0, le=365)
    priority: Priority = Priority.ROUTINE
    reason_refs: tuple[str, ...] = Field(
        default=(),
        max_length=10,
        description="Ids of Conditions or Observations in the evidence that justify the activity",
    )
    medication: MedicationProposal | None = None


class CarePlanProposal(BaseModel):
    """The JSON object the care-coordination model must return."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    goals: tuple[str, ...] = Field(min_length=1, max_length=10)
    activities: tuple[ProposedActivity, ...] = Field(min_length=1, max_length=25)
    rationale: str = Field(min_length=10, max_length=4000)


class ModelProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    request_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    attempts: int
    output_digest: str


class CarePlanDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int = Field(ge=0)
    proposal: CarePlanProposal
    drafted_by: str
    drafted_at: datetime
    evidence_digest: str
    model: ModelProvenance

    @property
    def digest(self) -> str:
        """What the clinician signs: the proposal plus the evidence it was drafted from."""
        return content_digest(
            canonical_json(
                {
                    "revision": self.revision,
                    "proposal": self.proposal.model_dump(mode="json"),
                    "evidence_digest": self.evidence_digest,
                }
            )
        )


class FindingSource(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL_REVIEW = "model-review"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable id: check plus subject; used to acknowledge the finding")
    check: str
    severity: Severity
    source: FindingSource
    message: str
    activity_index: int | None = None
    evidence_refs: tuple[str, ...] = ()


class ReviewStatus(StrEnum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"


class ValidationOutcome(StrEnum):
    PASSED = "passed"
    NEEDS_ATTENTION = "needs_attention"  # warnings only
    BLOCKED = "blocked"  # errors must be acknowledged by the signing clinician


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_digest: str
    ruleset_version: str
    findings: tuple[ValidationFinding, ...]
    review_status: ReviewStatus
    review_summary: str | None = None
    review_model: ModelProvenance | None = None
    validated_at: datetime

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def outcome(self) -> ValidationOutcome:
        if self.errors:
            return ValidationOutcome.BLOCKED
        if self.warnings:
            return ValidationOutcome.NEEDS_ATTENTION
        return ValidationOutcome.PASSED


class ModelConcern(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Severity
    category: str = Field(min_length=2, max_length=60)
    message: str = Field(min_length=5, max_length=1000)
    activity_index: int | None = Field(default=None, ge=0, le=24)


class ClinicalReview(BaseModel):
    """The JSON object the clinical-review model must return."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    concerns: tuple[ModelConcern, ...] = Field(default=(), max_length=20)
    summary: str = Field(min_length=5, max_length=2000)
