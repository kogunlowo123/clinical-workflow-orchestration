"""Care-coordination agent: drafts a care plan and task set from the encounter evidence.

The agent proposes; it never writes. Its output is a typed CarePlanProposal
that the validation agent checks and a clinician signs before anything
reaches the EHR.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from clinical_workflow_orchestration.agents.llm import StructuredModelClient
from clinical_workflow_orchestration.clinical.evidence import EvidencePacket
from clinical_workflow_orchestration.domain.careplan import CarePlanDraft, CarePlanProposal

TASK = "care-plan-draft"

SYSTEM_PROMPT = """You are the care-coordination agent of a hospital care-transitions team.
From the patient's record, draft a post-encounter care plan for a clinician to review.

Rules:
- Propose only what the record supports. Every follow-up, lab, referral or monitoring
  activity that addresses a condition or result must list that resource's id in reason_refs.
- Medication activities are proposals for clinician review: use kind "medication" with a
  medication object. stop/continue/adjust must name the existing active MedicationRequest id
  in existing_request_id; start needs the drug display and, when known, its RxNorm code.
- Labs use the LOINC code in "code".
- Schedule follow-up promptly for high-risk conditions (for heart failure within 7 days of
  discharge) and recheck abnormal results.
- Do not duplicate work already listed under open_work.
- You do not diagnose or prescribe; a clinician signs every plan."""


class CareCoordinationAgent:
    name = "care-coordination-agent"
    description = "Drafts post-encounter care plans and task sets from FHIR evidence."

    def __init__(self, model: StructuredModelClient, *, agent_id: str) -> None:
        self._model = model
        self.agent_id = agent_id

    async def draft(
        self,
        packet: EvidencePacket,
        *,
        revision: int,
        feedback: Sequence[str],
        now: datetime,
    ) -> CarePlanDraft:
        instruction = "Draft the care plan for this patient as specified."
        if feedback:
            notes = "\n".join(f"- {item}" for item in feedback)
            instruction = (
                "The reviewing clinician returned the previous draft with these requested "
                f"changes; produce a revised plan that addresses all of them:\n{notes}"
            )
        result = await self._model.complete(
            task=TASK,
            system=SYSTEM_PROMPT,
            instruction=instruction,
            record=json.dumps(packet.clinical_view(), sort_keys=True),
            schema=CarePlanProposal,
        )
        return CarePlanDraft(
            revision=revision,
            proposal=result.value,
            drafted_by=self.agent_id,
            drafted_at=now,
            evidence_digest=packet.digest,
            model=result.provenance,
        )
