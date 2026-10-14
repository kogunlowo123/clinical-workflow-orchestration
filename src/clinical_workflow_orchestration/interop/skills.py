"""Agent-facing skills shared by the MCP tools, the A2A endpoint and the Agent Framework adapter.

Every skill invocation is authorized for the calling agent (identity fabric
or local policy), counted, and recorded in the audit chain whether it was
allowed or not. Outputs follow minimum-necessary: agents see workflow
status, plan activities and finding summaries, never the evidence packet.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.observability import correlation_id
from agentic_core.policy import Resource
from pydantic import BaseModel, ConfigDict, Field

from clinical_workflow_orchestration.clinical.terminology import Severity
from clinical_workflow_orchestration.domain.careplan import MedicationProposal, ValidationFinding
from clinical_workflow_orchestration.domain.errors import ForbiddenError
from clinical_workflow_orchestration.domain.workflow import Workflow, WorkflowState
from clinical_workflow_orchestration.fhir.resources import FhirId
from clinical_workflow_orchestration.observability.metrics import OrchestrationMetrics
from clinical_workflow_orchestration.orchestration.service import ClinicalWorkflowService
from clinical_workflow_orchestration.security.agents import AgentAuthorizer

PurposeOfUse = Literal["TREAT", "HOPERAT"]
WORKFLOW_ID = r"^[0-9a-f]{32}$"


class Skill(StrEnum):
    START = "start-care-workflow"
    GET = "get-care-workflow"
    LIST_PENDING = "list-pending-approvals"
    CHECK_MEDICATIONS = "check-medication-safety"


class StartWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encounter_id: FhirId = Field(description="FHIR Encounter id the care plan follows from")
    purpose_of_use: PurposeOfUse
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class GetWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(pattern=WORKFLOW_ID)
    purpose_of_use: PurposeOfUse


class ListPendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose_of_use: PurposeOfUse
    limit: int = Field(default=20, ge=1, le=100)


class CheckMedicationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: FhirId
    purpose_of_use: PurposeOfUse
    proposals: list[MedicationProposal] = Field(min_length=1, max_length=25)


class ActivitySummary(BaseModel):
    kind: str
    description: str
    due_in_days: int
    priority: str


class WorkflowSummary(BaseModel):
    workflow_id: str
    state: WorkflowState
    encounter_id: str
    created_at: datetime
    updated_at: datetime
    revision: int
    draft_digest: str | None = None
    activities: list[ActivitySummary] = Field(default_factory=list)
    validation_outcome: str | None = None
    error_findings: int = 0
    warning_findings: int = 0
    care_plan: str | None = None
    failure_code: str | None = None

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowSummary:
        draft, report = workflow.draft, workflow.validation
        return cls(
            workflow_id=workflow.id,
            state=workflow.state,
            encounter_id=workflow.encounter_id,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
            revision=workflow.revision,
            draft_digest=draft.digest if draft else None,
            activities=[
                ActivitySummary(
                    kind=a.kind.value,
                    description=a.description,
                    due_in_days=a.due_in_days,
                    priority=a.priority.value,
                )
                for a in (draft.proposal.activities if draft else ())
            ],
            validation_outcome=report.outcome.value if report else None,
            error_findings=len(report.errors) if report else 0,
            warning_findings=len(report.warnings) if report else 0,
            care_plan=workflow.commit.care_plan if workflow.commit else None,
            failure_code=workflow.failure.code if workflow.failure else None,
        )


class PendingApprovals(BaseModel):
    items: list[WorkflowSummary]


class FindingSummary(BaseModel):
    id: str
    check: str
    severity: Severity
    message: str

    @classmethod
    def of(cls, finding: ValidationFinding) -> FindingSummary:
        return cls(
            id=finding.id, check=finding.check, severity=finding.severity, message=finding.message
        )


class MedicationCheckResult(BaseModel):
    patient_id: str
    safe_to_propose: bool
    findings: list[FindingSummary]


SKILL_ACTIONS: dict[Skill, tuple[str, str]] = {
    Skill.START: ("workflow:start", "workflow"),
    Skill.GET: ("workflow:read", "workflow"),
    Skill.LIST_PENDING: ("workflow:list", "workflow"),
    Skill.CHECK_MEDICATIONS: ("medication:check", "patient"),
}

SKILL_INPUTS: dict[Skill, type[BaseModel]] = {
    Skill.START: StartWorkflowInput,
    Skill.GET: GetWorkflowInput,
    Skill.LIST_PENDING: ListPendingInput,
    Skill.CHECK_MEDICATIONS: CheckMedicationsInput,
}

SKILL_DESCRIPTIONS: dict[Skill, str] = {
    Skill.START: (
        "Start the post-encounter care-coordination workflow for a FHIR Encounter: evidence "
        "retrieval, care-plan drafting, clinical validation, then clinician sign-off before "
        "anything is written to the EHR."
    ),
    Skill.GET: "Status, drafted activities and validation outcome of a care workflow.",
    Skill.LIST_PENDING: "Care workflows waiting for clinician sign-off.",
    Skill.CHECK_MEDICATIONS: (
        "Check proposed medication changes for a patient against active medications, "
        "allergies and renal function (duplicate therapy, interactions, contraindications)."
    ),
}


class SkillRouter:
    def __init__(
        self,
        service: ClinicalWorkflowService,
        authorizer: AgentAuthorizer,
        *,
        audit: AuditLog,
        metrics: OrchestrationMetrics,
    ) -> None:
        self._service = service
        self._authorizer = authorizer
        self._audit = audit
        self._metrics = metrics

    async def _authorize(
        self,
        skill: Skill,
        *,
        authorization: str | None,
        channel: str,
        resource_id: str,
        purpose_of_use: str,
    ) -> str:
        action, resource_type = SKILL_ACTIONS[skill]
        decision = await self._authorizer.authorize(
            authorization,
            action=action,
            resource=Resource(
                type=resource_type, id=resource_id, classification=DataClassification.RESTRICTED
            ),
            environment={"purpose_of_use": purpose_of_use, "channel": channel},
        )
        self._metrics.agent_calls.labels(channel, action, decision.decision.value).inc()
        allowed = decision.decision is Decision.ALLOW
        await self._audit.record(
            actor=decision.caller,
            action=f"agent.{action}",
            resource=f"{resource_type}/{resource_id}",
            outcome="success" if allowed else "denied",
            correlation_id=correlation_id(),
            details={
                "channel": channel,
                "decision": decision.decision.value,
                "rules": list(decision.matched_rules),
                "policy_digest": decision.policy_digest,
                "mode": decision.mode,
                "purpose_of_use": purpose_of_use,
            },
        )
        if not allowed:
            raise ForbiddenError(f"{action} {decision.decision.value}: {decision.reason}")
        return decision.caller

    async def start(
        self, request: StartWorkflowInput, *, authorization: str | None, channel: str
    ) -> WorkflowSummary:
        caller = await self._authorize(
            Skill.START,
            authorization=authorization,
            channel=channel,
            resource_id=request.encounter_id,
            purpose_of_use=request.purpose_of_use,
        )
        workflow, _ = await self._service.start(
            encounter_id=request.encounter_id,
            idempotency_key=request.idempotency_key,
            actor=caller,
        )
        return WorkflowSummary.of(workflow)

    async def get(
        self, request: GetWorkflowInput, *, authorization: str | None, channel: str
    ) -> WorkflowSummary:
        await self._authorize(
            Skill.GET,
            authorization=authorization,
            channel=channel,
            resource_id=request.workflow_id,
            purpose_of_use=request.purpose_of_use,
        )
        return WorkflowSummary.of(await self._service.get(request.workflow_id))

    async def list_pending(
        self, request: ListPendingInput, *, authorization: str | None, channel: str
    ) -> PendingApprovals:
        await self._authorize(
            Skill.LIST_PENDING,
            authorization=authorization,
            channel=channel,
            resource_id="*",
            purpose_of_use=request.purpose_of_use,
        )
        workflows = await self._service.list_workflows(
            state=WorkflowState.AWAITING_APPROVAL, limit=request.limit, offset=0
        )
        return PendingApprovals(items=[WorkflowSummary.of(w) for w in workflows])

    async def check_medications(
        self, request: CheckMedicationsInput, *, authorization: str | None, channel: str
    ) -> MedicationCheckResult:
        caller = await self._authorize(
            Skill.CHECK_MEDICATIONS,
            authorization=authorization,
            channel=channel,
            resource_id=request.patient_id,
            purpose_of_use=request.purpose_of_use,
        )
        findings = await self._service.check_medications(
            patient_id=request.patient_id, proposals=request.proposals, actor=caller
        )
        return MedicationCheckResult(
            patient_id=request.patient_id,
            safe_to_propose=not any(f.severity is Severity.ERROR for f in findings),
            findings=[FindingSummary.of(f) for f in findings],
        )

    async def invoke(
        self, skill: Skill, arguments: dict[str, Any], *, authorization: str | None, channel: str
    ) -> BaseModel:
        """Generic dispatch for transports that carry the skill name in the payload."""
        match skill:
            case Skill.START:
                return await self.start(
                    StartWorkflowInput.model_validate(arguments),
                    authorization=authorization,
                    channel=channel,
                )
            case Skill.GET:
                return await self.get(
                    GetWorkflowInput.model_validate(arguments),
                    authorization=authorization,
                    channel=channel,
                )
            case Skill.LIST_PENDING:
                return await self.list_pending(
                    ListPendingInput.model_validate(arguments),
                    authorization=authorization,
                    channel=channel,
                )
            case Skill.CHECK_MEDICATIONS:
                return await self.check_medications(
                    CheckMedicationsInput.model_validate(arguments),
                    authorization=authorization,
                    channel=channel,
                )
        raise ValueError(f"unsupported skill {skill}")  # pragma: no cover - exhaustive
