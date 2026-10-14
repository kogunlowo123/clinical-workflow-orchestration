"""Evidence retrieval, knowledge tables and the deterministic validation rules."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from clinical_workflow_orchestration.clinical.evidence import (
    EvidencePacket,
    EvidenceRetriever,
    age_band,
    parse_encounter,
    parse_observation,
)
from clinical_workflow_orchestration.clinical.rules import (
    build_regimen,
    latest_egfr,
    validate_medications,
    validate_plan,
)
from clinical_workflow_orchestration.clinical.terminology import (
    DrugClass,
    Severity,
    allergen_rules,
    classes_of,
    follow_up_rule,
    ingredients_of,
)
from clinical_workflow_orchestration.domain.careplan import (
    CarePlanProposal,
    MedicationAction,
    MedicationProposal,
)
from clinical_workflow_orchestration.domain.errors import FhirError
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.fhir.resources import (
    ICD10CM,
    RXNORM,
    SNOMED,
    CodeableConcept,
    Coding,
)
from tests.conftest import FHIR_BASE, RISKY_DRAFT, SAFE_DRAFT, FakeFhirServer

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def retriever(fhir: FakeFhirServer) -> EvidenceRetriever:
    client = FhirClient(
        FHIR_BASE, http=httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler))
    )
    return EvidenceRetriever(client, agent_id="spiffe://agents.test/agent/clinical/orchestrator")


@pytest.fixture
async def packet(fhir: FakeFhirServer) -> EvidencePacket:
    return await retriever(fhir).for_encounter("cwo-e1", now=NOW)


def proposal(base: dict[str, Any] | None = None, **changes: Any) -> CarePlanProposal:
    data = copy.deepcopy(base or SAFE_DRAFT)
    data.update(changes)
    return CarePlanProposal.model_validate(data)


def concept(text: str | None = None, *codings: tuple[str, str]) -> CodeableConcept:
    return CodeableConcept(coding=tuple(Coding(system=s, code=c) for s, c in codings), text=text)


class TestEvidence:
    async def test_collects_active_record(self, packet: EvidencePacket) -> None:
        assert packet.patient_id == "cwo-p1"
        assert packet.age_band == "70-79"
        assert packet.sex == "female"
        assert packet.encounter is not None
        assert packet.encounter.class_code == "IMP"
        assert packet.encounter.discharge_disposition == "Home"
        assert {c.id for c in packet.conditions} == {"cwo-c1", "cwo-c2", "cwo-c3"}
        assert {m.id for m in packet.medications} == {"cwo-m1", "cwo-m2", "cwo-m3", "cwo-m4"}
        atorvastatin = packet.medication("cwo-m4")
        assert atorvastatin is not None
        assert "atorvastatin" in atorvastatin.medication.label
        assert {a.id for a in packet.allergies} == {"cwo-a1"}  # refuted allergy dropped
        potassium = [
            o for o in packet.observations if o.code.codes("http://loinc.org") == ("2823-3",)
        ]
        assert [o.id for o in potassium] == ["cwo-o1"]  # latest per analyte only
        assert {w.id for w in packet.open_work} == {"cwo-t1"}
        assert len(packet.sources) >= 15
        assert all(s.digest.startswith("sha256:") for s in packet.sources)
        assert packet.digest.startswith("sha256:")

    async def test_model_view_is_deidentified(self, packet: EvidencePacket) -> None:
        text = json.dumps(packet.clinical_view())
        for identifier in ("Rowan", "Synthetic", "1951-03-14", "cwo-p1"):
            assert identifier not in text
        assert "Heart failure" in text
        assert "70-79" in text

    async def test_patient_only_packet(self, fhir: FakeFhirServer) -> None:
        packet = await retriever(fhir).for_patient("cwo-p1", now=NOW)
        assert packet.encounter is None
        assert packet.medications

    async def test_encounter_without_patient(self, fhir: FakeFhirServer) -> None:
        fhir.put({"resourceType": "Encounter", "id": "orphan", "status": "finished"})
        with pytest.raises(FhirError, match="no Patient subject"):
            await retriever(fhir).for_encounter("orphan", now=NOW)

    def test_age_bands(self) -> None:
        today = date(2026, 10, 14)
        assert age_band("1951-03-14", today) == "70-79"
        assert age_band("1930", today) == "90+"
        assert age_band("2026-12-01", today) is None
        assert age_band(None, today) is None
        assert age_band("abc", today) is None
        assert age_band("2016-10-15", today) == "0-9"

    def test_parsers_tolerate_sparse_resources(self) -> None:
        encounter = parse_encounter({"id": "e", "class": "IMP"})
        assert encounter.class_code is None
        assert encounter.status == "unknown"
        observation = parse_observation(
            {"id": "o", "valueQuantity": {"value": True}, "interpretation": [{"text": "H"}]}
        )
        assert observation.value is None
        assert observation.interpretation == "H"


class TestTerminology:
    def test_ingredient_lookup_by_code_and_name(self) -> None:
        by_code = ingredients_of(concept(None, (RXNORM, "29046")))
        by_name = ingredients_of(concept("Lisinopril 10 MG Oral Tablet"))
        assert {i.name for i in by_code} == {i.name for i in by_name} == {"lisinopril"}
        assert ingredients_of(concept("ramiprilat")) == frozenset()  # whole words only
        assert classes_of(ingredients_of(concept("warfarin and ibuprofen"))) == {
            DrugClass.ANTICOAGULANT,
            DrugClass.NSAID,
        }

    def test_allergen_rules(self) -> None:
        assert [r.name for r in allergen_rules(concept(None, (SNOMED, "764146007")))] == [
            "penicillins"
        ]
        assert [r.name for r in allergen_rules(concept("Sulfa drugs"))] == [
            "sulfonamide antibiotics"
        ]
        assert [r.name for r in allergen_rules(concept(None, (RXNORM, "723")))] == ["penicillins"]
        assert allergen_rules(concept("peanut")) == ()

    def test_follow_up_rules(self) -> None:
        rule = follow_up_rule(concept(None, (ICD10CM, "I50.22")))
        assert rule is not None
        assert rule.max_days == 7
        snomed = follow_up_rule(concept(None, (SNOMED, "44054006")))
        assert snomed is not None
        assert snomed.name == "type 2 diabetes mellitus"
        assert follow_up_rule(concept(None, (ICD10CM, "J18.9"))) is None


class TestRules:
    async def test_safe_plan_passes(self, packet: EvidencePacket) -> None:
        assert validate_plan(packet, proposal()) == []

    async def test_risky_plan_findings(self, packet: EvidencePacket) -> None:
        findings = validate_plan(packet, proposal(RISKY_DRAFT))
        by_check = {(f.check, f.severity) for f in findings}
        assert ("allergy", Severity.ERROR) in by_check  # amoxicillin vs penicillin allergy
        assert ("interaction", Severity.ERROR) in by_check  # losartan + lisinopril
        assert ("missing-follow-up", Severity.ERROR) in by_check  # heart failure, discharge
        assert ("missing-follow-up", Severity.WARNING) in by_check  # diabetes
        assert ("abnormal-lab", Severity.ERROR) in by_check  # potassium 5.8, no recheck
        assert findings[0].severity is Severity.ERROR  # errors sort first
        allergy = next(f for f in findings if f.check == "allergy")
        assert allergy.activity_index == 0
        assert "AllergyIntolerance/cwo-a1" in allergy.evidence_refs

    async def test_grounding(self, packet: EvidencePacket) -> None:
        data = copy.deepcopy(SAFE_DRAFT)
        data["activities"][0]["reason_refs"] = ["invented-condition"]
        data["activities"][4]["medication"]["existing_request_id"] = "not-active"
        data["activities"].append(
            {"kind": "medication", "description": "Medication change", "due_in_days": 0}
        )
        ids = {f.id for f in validate_plan(packet, proposal(data)) if f.check == "grounding"}
        assert ids == {
            "grounding:activity-0-invented-condition",
            "grounding:activity-4-medication-target",
            "grounding:activity-6-medication",
        }

    async def test_stop_removes_from_regimen_and_adjust_replaces(
        self, packet: EvidencePacket
    ) -> None:
        stop = MedicationProposal(
            action=MedicationAction.STOP, display="lisinopril", existing_request_id="cwo-m1"
        )
        start = MedicationProposal(action=MedicationAction.START, display="losartan 50 MG")
        assert not [
            f for f in validate_medications(packet, [stop, start]) if f.check == "interaction"
        ]
        adjust = MedicationProposal(
            action=MedicationAction.ADJUST,
            display="lisinopril 20 MG Oral Tablet",
            existing_request_id="cwo-m1",
        )
        regimen = build_regimen(packet, proposal(activities=[_med_activity(adjust)]))
        keys = {item.key for item in regimen}
        assert "MedicationRequest/cwo-m1" not in keys
        assert "proposal/0" in keys

    async def test_pre_existing_problems_are_warnings(self, packet: EvidencePacket) -> None:
        packet = packet.model_copy(
            update={
                "medications": (
                    *packet.medications,
                    packet.medications[0].model_copy(
                        update={
                            "id": "old-amox",
                            "medication": concept("amoxicillin 250 MG"),
                        }
                    ),
                )
            }
        )
        findings = validate_plan(packet, proposal())
        allergy = [f for f in findings if f.check == "allergy"]
        assert allergy
        assert all(f.severity is Severity.WARNING for f in allergy)

    async def test_cross_reactivity_warning(self, packet: EvidencePacket) -> None:
        cephalexin = MedicationProposal(action=MedicationAction.START, display="cephalexin 500 MG")
        findings = validate_medications(packet, [cephalexin])
        assert [(f.check, f.severity) for f in findings] == [("allergy", Severity.WARNING)]
        assert "cross-reactivity" in findings[0].message

    async def test_aspirin_allergy_blocks_aspirin_and_nsaids(self, packet: EvidencePacket) -> None:
        packet = packet.model_copy(
            update={
                "allergies": (
                    packet.allergies[0].model_copy(
                        update={"id": "asa", "code": concept("Aspirin")}
                    ),
                )
            }
        )
        findings = validate_medications(
            packet,
            [
                MedicationProposal(action=MedicationAction.START, display="aspirin 81 MG"),
                MedicationProposal(action=MedicationAction.START, display="naproxen 250 MG"),
            ],
        )
        assert sum(1 for f in findings if f.check == "allergy") == 2

    async def test_intentional_combination_is_a_warning(self, packet: EvidencePacket) -> None:
        findings = validate_medications(
            packet,
            [
                MedicationProposal(action=MedicationAction.START, display="aspirin 81 MG"),
                MedicationProposal(action=MedicationAction.START, display="clopidogrel 75 MG"),
            ],
        )
        duplicate = next(f for f in findings if f.check == "duplicate-therapy")
        assert duplicate.severity is Severity.WARNING

    async def test_ingredient_level_interaction(self, packet: EvidencePacket) -> None:
        findings = validate_medications(
            packet,
            [
                MedicationProposal(action=MedicationAction.START, display="clopidogrel 75 MG"),
                MedicationProposal(
                    action=MedicationAction.START, display="omeprazole 20 MG", rxnorm_code="7646"
                ),
            ],
        )
        assert any("CYP2C19" in f.message for f in findings)

    async def test_renal_dosing(self, packet: EvidencePacket) -> None:
        egfr = latest_egfr(packet)
        assert egfr is not None
        assert egfr.value == 38
        start = MedicationProposal(action=MedicationAction.START, display="metformin 500 MG")
        caution = [f for f in validate_medications(packet, [start]) if f.check == "renal-dosing"]
        assert [f.severity for f in caution] == [Severity.WARNING]
        severe = packet.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(update={"value": 22.0}) if o.id == egfr.id else o
                    for o in packet.observations
                )
            }
        )
        errors = [f for f in validate_plan(severe, proposal()) if f.check == "renal-dosing"]
        assert [f.severity for f in errors] == [Severity.ERROR]
        assert "stop or replace" in errors[0].message

    async def test_no_egfr_no_renal_findings(self, packet: EvidencePacket) -> None:
        stripped = packet.model_copy(update={"observations": ()})
        assert latest_egfr(stripped) is None
        assert not [f for f in validate_plan(stripped, proposal()) if f.check == "renal-dosing"]

    async def test_low_potassium_and_hba1c(self, packet: EvidencePacket) -> None:
        changed = packet.model_copy(
            update={
                "observations": tuple(
                    (
                        o.model_copy(update={"value": 2.7})
                        if o.id == "cwo-o1"
                        else o.model_copy(update={"value": 10.4}) if o.id == "cwo-o4" else o
                    )
                    for o in packet.observations
                )
            }
        )
        data = copy.deepcopy(SAFE_DRAFT)
        data["activities"][1]["due_in_days"] = 7  # too late for a potassium recheck
        findings = {f.id: f for f in validate_plan(changed, proposal(data))}
        assert "low" in findings["abnormal-lab:Observation/cwo-o1"].message
        assert findings["abnormal-lab:Observation/cwo-o4"].severity is Severity.WARNING

    async def test_discharge_follow_up_requirement(self, packet: EvidencePacket) -> None:
        data = copy.deepcopy(SAFE_DRAFT)
        data["activities"] = [a for a in data["activities"] if a["kind"] != "follow-up"]
        ids = {f.id for f in validate_plan(packet, proposal(data))}
        assert "missing-follow-up:Encounter/cwo-e1" in ids
        assert "missing-follow-up:Condition/cwo-c1" in ids


def _med_activity(med: MedicationProposal) -> dict[str, Any]:
    return {
        "kind": "medication",
        "description": f"{med.action.value} {med.display}",
        "due_in_days": 0,
        "medication": med.model_dump(mode="json"),
    }
