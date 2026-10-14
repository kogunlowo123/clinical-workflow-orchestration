"""Build the FHIR R4 transaction written after clinician sign-off.

One atomic transaction carries:

* a ``CarePlan`` (status active, intent plan) authored by the signing
  practitioner, with the drafting agent as contributor;
* one ``Task`` per activity (status requested, intent order), linked from
  ``CarePlan.activity.reference`` and back via ``Task.basedOn``;
* a ``Provenance`` targeting every created resource, naming the agent as
  author, the practitioner as verifier, the evidence as source entities, and
  carrying the sign-off JWS as a Verification Signature.

CarePlan and Tasks use conditional create on a workflow-scoped identifier so
a retried commit cannot create duplicates; full URLs are deterministic
``urn:uuid`` values derived from the workflow id.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta

from agentic_core.domain.provenance import canonical_json, content_digest

from clinical_workflow_orchestration.clinical.evidence import EvidencePacket
from clinical_workflow_orchestration.domain.careplan import (
    ActivityKind,
    CarePlanDraft,
    ProposedActivity,
)
from clinical_workflow_orchestration.domain.workflow import SignOff
from clinical_workflow_orchestration.fhir.resources import (
    LOINC,
    PROVENANCE_PARTICIPANT,
    SIGNATURE_TYPE,
    TASK_CODE,
    V3_DATA_OPERATION,
    VERIFICATION_SIGNATURE,
    JsonObject,
)

MAX_SOURCE_ENTITIES = 100


def _urn(workflow_id: str, suffix: str) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'cwo:{workflow_id}:{suffix}')}"


def careplan_identifier(workflow_id: str) -> str:
    return f"{workflow_id}-careplan"


def task_identifier(workflow_id: str, index: int) -> str:
    return f"{workflow_id}-task-{index}"


def _reason_reference(packet: EvidencePacket, ref: str) -> JsonObject | None:
    if packet.condition(ref) is not None:
        return {"reference": f"Condition/{ref}"}
    if packet.observation(ref) is not None:
        return {"reference": f"Observation/{ref}"}
    return None


def _task(
    *,
    workflow_id: str,
    index: int,
    activity: ProposedActivity,
    packet: EvidencePacket,
    identifier_system: str,
    careplan_urn: str,
    practitioner: str,
    authored: datetime,
) -> JsonObject:
    task: JsonObject = {
        "resourceType": "Task",
        "identifier": [{"system": identifier_system, "value": task_identifier(workflow_id, index)}],
        "basedOn": [{"reference": careplan_urn}],
        "status": "requested",
        "intent": "order",
        "priority": activity.priority.value,
        "code": {
            "coding": [
                {"system": TASK_CODE, "code": "fulfill", "display": "Fulfill the focal request"}
            ],
            "text": activity.kind.value,
        },
        "description": activity.description,
        "for": {"reference": f"Patient/{packet.patient_id}"},
        "authoredOn": authored.isoformat(),
        "requester": {"reference": practitioner},
        "restriction": {
            "period": {"end": (authored + timedelta(days=activity.due_in_days)).date().isoformat()}
        },
    }
    if packet.encounter is not None:
        task["encounter"] = {"reference": f"Encounter/{packet.encounter.id}"}
    reasons = [r for r in (_reason_reference(packet, ref) for ref in activity.reason_refs) if r]
    if reasons:
        task["reasonReference"] = reasons[0]
    if activity.kind is ActivityKind.LAB and activity.code:
        task["input"] = [
            {
                "type": {"text": "laboratory test"},
                "valueCodeableConcept": {"coding": [{"system": LOINC, "code": activity.code}]},
            }
        ]
    med = activity.medication
    if med is not None:
        task["note"] = [{"text": f"Medication {med.action.value}: {med.display}"}]
        if med.existing_request_id:
            task["focus"] = {"reference": f"MedicationRequest/{med.existing_request_id}"}
    return task


def build_commit_bundle(
    *,
    workflow_id: str,
    packet: EvidencePacket,
    draft: CarePlanDraft,
    signoff: SignOff,
    identifier_system: str,
    agent_id: str,
    orchestrator_id: str,
) -> JsonObject:
    if signoff.signature is None:
        raise ValueError("sign-off must be signed before it can be committed")
    proposal = draft.proposal
    at = signoff.signed_at
    careplan_urn = _urn(workflow_id, "careplan")
    task_urns = [_urn(workflow_id, f"task-{i}") for i in range(len(proposal.activities))]
    provenance_urn = _urn(workflow_id, "provenance")

    addresses: list[JsonObject] = []
    for activity in proposal.activities:
        for ref in activity.reason_refs:
            target = {"reference": f"Condition/{ref}"}
            if packet.condition(ref) is not None and target not in addresses:
                addresses.append(target)

    careplan: JsonObject = {
        "resourceType": "CarePlan",
        "identifier": [{"system": identifier_system, "value": careplan_identifier(workflow_id)}],
        "status": "active",
        "intent": "plan",
        "title": proposal.title,
        "description": proposal.rationale,
        "subject": {"reference": f"Patient/{packet.patient_id}"},
        "period": {"start": at.date().isoformat()},
        "created": at.isoformat(),
        "author": {"reference": signoff.practitioner},
        "contributor": [
            {
                "identifier": {"system": "urn:ietf:rfc:3986", "value": agent_id},
                "display": "care-coordination-agent",
            }
        ],
        "activity": [{"reference": {"reference": urn}} for urn in task_urns],
        "note": [{"text": f"Goal: {goal}"} for goal in proposal.goals],
    }
    if packet.encounter is not None:
        careplan["encounter"] = {"reference": f"Encounter/{packet.encounter.id}"}
    if addresses:
        careplan["addresses"] = addresses

    entries: list[JsonObject] = [
        {
            "fullUrl": careplan_urn,
            "resource": careplan,
            "request": {
                "method": "POST",
                "url": "CarePlan",
                "ifNoneExist": f"identifier={identifier_system}|{careplan_identifier(workflow_id)}",
            },
        }
    ]
    for index, (activity, urn) in enumerate(zip(proposal.activities, task_urns, strict=True)):
        entries.append(
            {
                "fullUrl": urn,
                "resource": _task(
                    workflow_id=workflow_id,
                    index=index,
                    activity=activity,
                    packet=packet,
                    identifier_system=identifier_system,
                    careplan_urn=careplan_urn,
                    practitioner=signoff.practitioner,
                    authored=at,
                ),
                "request": {
                    "method": "POST",
                    "url": "Task",
                    "ifNoneExist": (
                        f"identifier={identifier_system}|{task_identifier(workflow_id, index)}"
                    ),
                },
            }
        )

    entities: list[JsonObject] = [
        {
            "role": "source",
            "what": {
                "reference": (
                    f"{source.reference}/_history/{source.version_id}"
                    if source.version_id
                    else source.reference
                )
            },
        }
        for source in packet.sources[:MAX_SOURCE_ENTITIES]
    ]
    provenance: JsonObject = {
        "resourceType": "Provenance",
        "target": [{"reference": careplan_urn}, *({"reference": u} for u in task_urns)],
        "recorded": at.isoformat(),
        "activity": {"coding": [{"system": V3_DATA_OPERATION, "code": "CREATE"}]},
        "reason": [{"text": f"Clinician-signed care plan from workflow {workflow_id}"}],
        "agent": [
            {
                "type": {"coding": [{"system": PROVENANCE_PARTICIPANT, "code": "author"}]},
                "who": {
                    "identifier": {"system": "urn:ietf:rfc:3986", "value": agent_id},
                    "display": "care-coordination-agent",
                },
            },
            {
                "type": {"coding": [{"system": PROVENANCE_PARTICIPANT, "code": "verifier"}]},
                "who": {"reference": signoff.practitioner},
            },
            {
                "type": {"coding": [{"system": PROVENANCE_PARTICIPANT, "code": "assembler"}]},
                "who": {
                    "identifier": {"system": "urn:ietf:rfc:3986", "value": orchestrator_id},
                    "display": "clinical-workflow-orchestrator",
                },
            },
        ],
        "entity": entities,
        "signature": [
            {
                "type": [
                    {
                        "system": SIGNATURE_TYPE,
                        "code": VERIFICATION_SIGNATURE,
                        "display": "Verification Signature",
                    }
                ],
                "when": at.isoformat(),
                "who": {"reference": signoff.practitioner},
                "targetFormat": "application/fhir+json",
                "sigFormat": "application/jose",
                "data": base64.b64encode(signoff.signature.encode()).decode(),
            }
        ],
    }
    entries.append(
        {
            "fullUrl": provenance_urn,
            "resource": provenance,
            "request": {"method": "POST", "url": "Provenance"},
        }
    )
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def bundle_digest(bundle: JsonObject) -> str:
    return content_digest(canonical_json(bundle))
