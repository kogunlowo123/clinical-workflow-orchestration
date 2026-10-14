"""Clinical validation agent: deterministic safety checks, then a model review.

Deterministic rules run first and are authoritative: their errors can only
be cleared by the signing clinician. The model review looks for problems
rules cannot express (an implausible goal, a missing education item); its
concerns are capped at WARNING so a model can neither block nor clear a
plan. If every model provider is unavailable the deterministic report is
still produced and the review is marked unavailable for the clinician.
"""

from __future__ import annotations

import json
from datetime import datetime

from agentic_core.domain.errors import ProviderUnavailableError

from clinical_workflow_orchestration.agents.llm import StructuredModelClient
from clinical_workflow_orchestration.clinical.evidence import EvidencePacket
from clinical_workflow_orchestration.clinical.rules import validate_plan
from clinical_workflow_orchestration.clinical.terminology import RULESET_VERSION, Severity
from clinical_workflow_orchestration.domain.careplan import (
    CarePlanDraft,
    ClinicalReview,
    FindingSource,
    ReviewStatus,
    ValidationFinding,
    ValidationReport,
)
from clinical_workflow_orchestration.domain.errors import ModelOutputError

TASK = "clinical-review"

SYSTEM_PROMPT = """You are the clinical validation agent reviewing a drafted care plan
before a clinician signs it. Deterministic checks have already covered duplicate therapy,
interactions, allergies, renal dosing, follow-up timing and abnormal-result rechecks; their
findings are listed. Look for remaining problems: activities that do not fit the record,
missing patient education or monitoring, unclear or unsafe instructions, goals that do not
match the activities. Report each concern with severity "warning" or "info" and the index of
the activity it concerns. If there is nothing to add, return an empty concerns list."""


class ClinicalValidationAgent:
    name = "clinical-validation-agent"
    description = "Validates drafted care plans with deterministic safety rules and model review."

    def __init__(self, model: StructuredModelClient, *, agent_id: str) -> None:
        self._model = model
        self.agent_id = agent_id

    async def validate(
        self, packet: EvidencePacket, draft: CarePlanDraft, *, now: datetime
    ) -> ValidationReport:
        findings = validate_plan(packet, draft.proposal)
        record = json.dumps(
            {
                "patient_record": packet.clinical_view(),
                "draft_plan": draft.proposal.model_dump(mode="json"),
                "deterministic_findings": [
                    f.model_dump(mode="json", include={"check", "severity", "message"})
                    for f in findings
                ],
            },
            sort_keys=True,
        )
        try:
            review = await self._model.complete(
                task=TASK,
                system=SYSTEM_PROMPT,
                instruction="Review the draft plan against the record.",
                record=record,
                schema=ClinicalReview,
            )
        except (ProviderUnavailableError, ModelOutputError) as exc:
            return ValidationReport(
                draft_digest=draft.digest,
                ruleset_version=RULESET_VERSION,
                findings=(
                    *findings,
                    ValidationFinding(
                        id="model-review:unavailable",
                        check="model-review",
                        severity=Severity.INFO,
                        source=FindingSource.MODEL_REVIEW,
                        message=f"Model review unavailable ({type(exc).__name__}); deterministic "
                        "checks only",
                    ),
                ),
                review_status=ReviewStatus.UNAVAILABLE,
                validated_at=now,
            )
        activity_count = len(draft.proposal.activities)
        concerns = [
            ValidationFinding(
                id=f"model-review:{index}",
                check=f"model-review:{concern.category}",
                severity=Severity.INFO if concern.severity is Severity.INFO else Severity.WARNING,
                source=FindingSource.MODEL_REVIEW,
                message=concern.message,
                activity_index=(
                    concern.activity_index
                    if concern.activity_index is not None
                    and concern.activity_index < activity_count
                    else None
                ),
            )
            for index, concern in enumerate(review.value.concerns)
        ]
        return ValidationReport(
            draft_digest=draft.digest,
            ruleset_version=RULESET_VERSION,
            findings=(*findings, *concerns),
            review_status=ReviewStatus.COMPLETED,
            review_summary=review.value.summary,
            review_model=review.provenance,
            validated_at=now,
        )
