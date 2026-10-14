"""Evidence retrieval: the clinical facts an encounter's care plan is built and checked against.

The retriever reads the encounter and the patient's active problems,
medications, allergies, recent laboratory results and open care plans and
tasks from FHIR, normalises them into typed facts, and records a provenance
digest for every source resource so each downstream decision can be traced
to the exact data it saw.

The packet is de-identified by construction: it carries FHIR logical ids,
codes, values, an age band and administrative sex, never names, addresses,
contact details or business identifiers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.domain.provenance import ProvenanceRecord, canonical_json, content_digest
from pydantic import BaseModel, ConfigDict

from clinical_workflow_orchestration.domain.errors import FhirError
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.fhir.resources import (
    CodeableConcept,
    JsonObject,
    as_float,
    as_str,
    first_codeable_concept,
    parse_codeable_concept,
    reference_id,
    status_code,
)

INACTIVE_ALLERGY_VERIFICATION = frozenset({"refuted", "entered-in-error"})
INACTIVE_CLINICAL = frozenset({"inactive", "resolved", "remission"})


class EncounterFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    status: str
    class_code: str | None
    type: CodeableConcept
    period_start: str | None
    period_end: str | None
    discharge_disposition: str | None


class ConditionFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    code: CodeableConcept
    clinical_status: str | None
    onset: str | None


class MedicationFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    medication: CodeableConcept
    status: str
    dosage: str | None
    authored_on: str | None


class ObservationFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    code: CodeableConcept
    value: float | None
    unit: str | None
    effective: str | None
    interpretation: str | None


class AllergyFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    code: CodeableConcept
    criticality: str | None
    clinical_status: str | None
    verification_status: str | None
    reactions: tuple[str, ...] = ()


class OpenWorkFact(BaseModel):
    """An existing active CarePlan or open Task, so drafts do not duplicate work."""

    model_config = ConfigDict(frozen=True)

    id: str
    resource_type: str
    status: str
    description: str


class SourceDigest(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    digest: str
    version_id: str | None = None


class EvidencePacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    patient_id: str
    age_band: str | None
    sex: str | None
    encounter: EncounterFact | None
    conditions: tuple[ConditionFact, ...] = ()
    medications: tuple[MedicationFact, ...] = ()
    observations: tuple[ObservationFact, ...] = ()
    allergies: tuple[AllergyFact, ...] = ()
    open_work: tuple[OpenWorkFact, ...] = ()
    sources: tuple[SourceDigest, ...] = ()
    retrieved_at: datetime
    retrieved_by: str

    @property
    def digest(self) -> str:
        return content_digest(canonical_json(self.model_dump(mode="json")))

    def condition(self, condition_id: str) -> ConditionFact | None:
        return next((c for c in self.conditions if c.id == condition_id), None)

    def medication(self, request_id: str) -> MedicationFact | None:
        return next((m for m in self.medications if m.id == request_id), None)

    def observation(self, observation_id: str) -> ObservationFact | None:
        return next((o for o in self.observations if o.id == observation_id), None)

    def clinical_view(self) -> dict[str, Any]:
        """The model-facing view: clinical content only, no source digests or patient id."""
        return {
            "patient": {"age_band": self.age_band, "sex": self.sex},
            "encounter": (
                self.encounter.model_dump(mode="json", exclude={"id"}) if self.encounter else None
            ),
            "conditions": [_compact(c) for c in self.conditions],
            "active_medications": [_compact(m) for m in self.medications],
            "recent_labs": [_compact(o) for o in self.observations],
            "allergies": [_compact(a) for a in self.allergies],
            "open_work": [_compact(w) for w in self.open_work],
        }


def _compact(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True, exclude_defaults=True) | {
        "id": getattr(model, "id", None)
    }


def age_band(birth_date: str | None, today: date) -> str | None:
    """Ten-year age band; 90+ collapsed as HIPAA Safe Harbor requires."""
    if not birth_date or len(birth_date) < 4 or not birth_date[:4].isdigit():
        return None
    year = int(birth_date[:4])
    month, day = 7, 1  # mid-year when only the year is known
    if len(birth_date) >= 10:
        month, day = int(birth_date[5:7]), int(birth_date[8:10])
    age = today.year - year - ((today.month, today.day) < (month, day))
    if age < 0:
        return None
    if age >= 90:
        return "90+"
    low = (age // 10) * 10
    return f"{low}-{low + 9}"


# ---------------------------------------------------------------------- parsing


def parse_encounter(resource: JsonObject) -> EncounterFact:
    klass = resource.get("class")
    period = resource.get("period") or {}
    hospitalization = resource.get("hospitalization") or {}
    return EncounterFact(
        id=str(resource["id"]),
        status=str(resource.get("status") or "unknown"),
        class_code=as_str(klass.get("code")) if isinstance(klass, dict) else None,
        type=first_codeable_concept(resource.get("type")),
        period_start=as_str(period.get("start")) if isinstance(period, dict) else None,
        period_end=as_str(period.get("end")) if isinstance(period, dict) else None,
        discharge_disposition=(
            parse_codeable_concept(hospitalization.get("dischargeDisposition")).label
            if isinstance(hospitalization, dict) and hospitalization.get("dischargeDisposition")
            else None
        ),
    )


def parse_condition(resource: JsonObject) -> ConditionFact:
    return ConditionFact(
        id=str(resource["id"]),
        code=parse_codeable_concept(resource.get("code")),
        clinical_status=status_code(resource.get("clinicalStatus")),
        onset=as_str(resource.get("onsetDateTime")),
    )


def parse_medication_request(
    resource: JsonObject, medications: dict[str, CodeableConcept]
) -> MedicationFact:
    concept = parse_codeable_concept(resource.get("medicationCodeableConcept"))
    if not concept.coding and not concept.text:
        med_id = reference_id(resource.get("medicationReference"), "Medication")
        if med_id is not None and med_id in medications:
            concept = medications[med_id]
    dosage = None
    for instruction in resource.get("dosageInstruction") or []:
        if isinstance(instruction, dict) and isinstance(instruction.get("text"), str):
            dosage = instruction["text"]
            break
    return MedicationFact(
        id=str(resource["id"]),
        medication=concept,
        status=str(resource.get("status") or "unknown"),
        dosage=dosage,
        authored_on=as_str(resource.get("authoredOn")),
    )


def parse_observation(resource: JsonObject) -> ObservationFact:
    quantity = resource.get("valueQuantity")
    value = unit = None
    if isinstance(quantity, dict):
        value = as_float(quantity.get("value"))
        unit = as_str(quantity.get("unit")) or as_str(quantity.get("code"))
    interpretation = None
    interpretations = resource.get("interpretation")
    if isinstance(interpretations, list) and interpretations:
        interpretation = status_code(interpretations[0])
    return ObservationFact(
        id=str(resource["id"]),
        code=parse_codeable_concept(resource.get("code")),
        value=value,
        unit=unit,
        effective=as_str(resource.get("effectiveDateTime")) or as_str(resource.get("issued")),
        interpretation=interpretation,
    )


def parse_allergy(resource: JsonObject) -> AllergyFact:
    reactions: list[str] = []
    for reaction in resource.get("reaction") or []:
        if isinstance(reaction, dict):
            for manifestation in reaction.get("manifestation") or []:
                reactions.append(parse_codeable_concept(manifestation).label)
    return AllergyFact(
        id=str(resource["id"]),
        code=parse_codeable_concept(resource.get("code")),
        criticality=as_str(resource.get("criticality")),
        clinical_status=status_code(resource.get("clinicalStatus")),
        verification_status=status_code(resource.get("verificationStatus")),
        reactions=tuple(reactions),
    )


def allergy_is_active(allergy: AllergyFact) -> bool:
    if allergy.verification_status in INACTIVE_ALLERGY_VERIFICATION:
        return False
    return allergy.clinical_status not in INACTIVE_CLINICAL


def _latest_per_code(observations: list[ObservationFact]) -> list[ObservationFact]:
    latest: dict[str, ObservationFact] = {}
    for obs in observations:
        key = obs.code.coding[0].code if obs.code.coding and obs.code.coding[0].code else obs.id
        current = latest.get(key)
        if current is None or (obs.effective or "") > (current.effective or ""):
            latest[key] = obs
    return sorted(latest.values(), key=lambda o: o.effective or "", reverse=True)


# --------------------------------------------------------------------- retrieval


class EvidenceRetriever:
    def __init__(
        self,
        fhir: FhirClient,
        *,
        agent_id: str,
        max_pages: int = 20,
        max_observations: int = 200,
    ) -> None:
        self._fhir = fhir
        self._agent_id = agent_id
        self._max_pages = max_pages
        self._max_observations = max_observations

    async def for_encounter(self, encounter_id: str, *, now: datetime) -> EvidencePacket:
        encounter = await self._fhir.read("Encounter", encounter_id)
        patient_id = reference_id(encounter.get("subject"), "Patient")
        if patient_id is None:
            raise FhirError(f"Encounter/{encounter_id} has no Patient subject", retryable=False)
        return await self._collect(patient_id, encounter, now=now)

    async def for_patient(self, patient_id: str, *, now: datetime) -> EvidencePacket:
        return await self._collect(patient_id, None, now=now)

    async def _collect(
        self, patient_id: str, encounter: JsonObject | None, *, now: datetime
    ) -> EvidencePacket:
        def search(resource_type: str, **params: str) -> Any:
            return self._fhir.search(
                resource_type, {"patient": patient_id, **params}, max_pages=self._max_pages
            )

        (
            patient,
            conditions,
            med_requests,
            observations,
            allergies,
            care_plans,
            tasks,
        ) = await asyncio.gather(
            self._fhir.read("Patient", patient_id),
            search("Condition", **{"clinical-status": "active"}),
            search("MedicationRequest", status="active"),
            self._fhir.search(
                "Observation",
                {"patient": patient_id, "category": "laboratory", "_sort": "-date"},
                max_pages=self._max_pages,
                max_resources=self._max_observations,
            ),
            search("AllergyIntolerance"),
            search("CarePlan", status="active"),
            search("Task", status="requested,accepted,in-progress"),
        )
        medications = await self._medications(med_requests)
        sources = [_digest(patient)]
        sources.extend(
            _digest(r)
            for r in [
                *([encounter] if encounter else []),
                *conditions,
                *med_requests,
                *observations,
                *allergies,
                *care_plans,
                *tasks,
            ]
        )
        allergy_facts = tuple(a for a in map(parse_allergy, allergies) if allergy_is_active(a))
        return EvidencePacket(
            patient_id=patient_id,
            age_band=age_band(as_str(patient.get("birthDate")), now.astimezone(UTC).date()),
            sex=as_str(patient.get("gender")),
            encounter=parse_encounter(encounter) if encounter else None,
            conditions=tuple(
                c
                for c in map(parse_condition, conditions)
                if c.clinical_status not in INACTIVE_CLINICAL
            ),
            medications=tuple(parse_medication_request(r, medications) for r in med_requests),
            observations=tuple(_latest_per_code([parse_observation(o) for o in observations])),
            allergies=allergy_facts,
            open_work=tuple(
                [
                    OpenWorkFact(
                        id=str(p["id"]),
                        resource_type="CarePlan",
                        status=str(p.get("status") or "unknown"),
                        description=str(p.get("title") or p.get("description") or "care plan"),
                    )
                    for p in care_plans
                ]
                + [
                    OpenWorkFact(
                        id=str(t["id"]),
                        resource_type="Task",
                        status=str(t.get("status") or "unknown"),
                        description=str(
                            t.get("description") or parse_codeable_concept(t.get("code")).label
                        ),
                    )
                    for t in tasks
                ]
            ),
            sources=tuple(sources),
            retrieved_at=now,
            retrieved_by=self._agent_id,
        )

    async def _medications(self, requests: list[JsonObject]) -> dict[str, CodeableConcept]:
        """Resolve ``medicationReference`` targets the server did not include."""
        wanted = {
            med_id
            for r in requests
            if not r.get("medicationCodeableConcept")
            and (med_id := reference_id(r.get("medicationReference"), "Medication"))
        }
        resolved: dict[str, CodeableConcept] = {}
        for med_id in sorted(wanted):
            medication = await self._fhir.read("Medication", med_id)
            resolved[med_id] = parse_codeable_concept(medication.get("code"))
        return resolved


def _digest(resource: JsonObject) -> SourceDigest:
    record = ProvenanceRecord.for_content(
        canonical_json(resource),
        source_uri=f"{resource.get('resourceType')}/{resource.get('id')}",
        source_type="fhir",
        trust=TrustLevel.VERIFIED,
        classification=DataClassification.RESTRICTED,
        retrieved_by="evidence-retriever",
    )
    meta = resource.get("meta")
    version = as_str(meta.get("versionId")) if isinstance(meta, dict) else None
    return SourceDigest(reference=record.source_uri, digest=record.digest, version_id=version)
