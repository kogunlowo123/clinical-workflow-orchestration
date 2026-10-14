"""Deterministic clinical validation of a proposed care plan against the evidence.

Checks, each producing findings with a stable id the signing clinician can
acknowledge:

* ``grounding``          activity references and medication targets exist in the evidence
* ``duplicate-therapy``  two medications with the same ingredient or therapeutic class
* ``interaction``        clinically significant class or ingredient combinations
* ``allergy``            medications contraindicated by an active AllergyIntolerance
* ``missing-follow-up``  conditions and discharges without timely follow-up
* ``abnormal-lab``       out-of-range results without a timely recheck
* ``renal-dosing``       metformin against the latest eGFR

Severity rule: a problem the plan *introduces or endorses* (start, adjust,
continue) is an error; a pre-existing problem the plan leaves untouched is a
warning, unless leaving it untouched is itself unsafe (renal dosing).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations

from clinical_workflow_orchestration.clinical.evidence import EvidencePacket, ObservationFact
from clinical_workflow_orchestration.clinical.terminology import (
    DISCHARGE_ENCOUNTER_CLASSES,
    EGFR,
    INTERACTIONS,
    LAB_RULES,
    METFORMIN_CAUTION_EGFR,
    METFORMIN_CONTRAINDICATED_EGFR,
    POST_DISCHARGE_FOLLOW_UP_DAYS,
    DrugClass,
    Ingredient,
    Severity,
    allergen_rules,
    classes_of,
    follow_up_rule,
    ingredients_of,
    interaction_keys,
    loinc_codes,
)
from clinical_workflow_orchestration.domain.careplan import (
    ActivityKind,
    CarePlanProposal,
    FindingSource,
    MedicationAction,
    MedicationProposal,
    ProposedActivity,
    ValidationFinding,
)
from clinical_workflow_orchestration.fhir.resources import RXNORM, CodeableConcept, Coding

# Classes where combining two members is usually intentional (e.g. dual
# antiplatelet therapy after stenting): duplication is a warning, not an error.
INTENTIONAL_COMBINATIONS = frozenset({DrugClass.ANTIPLATELET})
FOLLOW_UP_KINDS = frozenset({ActivityKind.FOLLOW_UP, ActivityKind.REFERRAL})


@dataclass(frozen=True)
class RegimenItem:
    """A medication the patient will be on if the plan is signed."""

    key: str  # MedicationRequest/<id> or proposal/<activity index>
    label: str
    ingredients: frozenset[Ingredient]
    endorsed: bool  # started, adjusted or explicitly continued by the plan
    activity_index: int | None

    @property
    def classes(self) -> frozenset[DrugClass]:
        return classes_of(self.ingredients)


def _finding(
    check: str,
    subject: str,
    severity: Severity,
    message: str,
    *,
    activity_index: int | None = None,
    refs: Iterable[str] = (),
) -> ValidationFinding:
    return ValidationFinding(
        id=f"{check}:{subject}",
        check=check,
        severity=severity,
        source=FindingSource.DETERMINISTIC,
        message=message,
        activity_index=activity_index,
        evidence_refs=tuple(refs),
    )


def build_regimen(packet: EvidencePacket, proposal: CarePlanProposal) -> list[RegimenItem]:
    stopped: set[str] = set()
    endorsed: dict[str, int] = {}
    items: list[RegimenItem] = []
    for index, activity in enumerate(proposal.activities):
        med = activity.medication
        if activity.kind is not ActivityKind.MEDICATION or med is None:
            continue
        if med.action is MedicationAction.STOP and med.existing_request_id:
            stopped.add(med.existing_request_id)
        elif med.action in {MedicationAction.CONTINUE, MedicationAction.ADJUST} and (
            med.existing_request_id
        ):
            endorsed[med.existing_request_id] = index
        if med.action in {MedicationAction.START, MedicationAction.ADJUST}:
            codings = (Coding(system=RXNORM, code=med.rxnorm_code),) if med.rxnorm_code else ()
            ingredients = ingredients_of(CodeableConcept(coding=codings, text=med.display))
            if med.action is MedicationAction.ADJUST and med.existing_request_id:
                stopped.add(med.existing_request_id)  # replaced by the adjusted order
            items.append(
                RegimenItem(
                    key=f"proposal/{index}",
                    label=med.display,
                    ingredients=ingredients,
                    endorsed=True,
                    activity_index=index,
                )
            )
    for med_fact in packet.medications:
        if med_fact.id in stopped:
            continue
        items.append(
            RegimenItem(
                key=f"MedicationRequest/{med_fact.id}",
                label=med_fact.medication.label,
                ingredients=ingredients_of(med_fact.medication),
                endorsed=med_fact.id in endorsed,
                activity_index=endorsed.get(med_fact.id),
            )
        )
    return items


def check_grounding(packet: EvidencePacket, proposal: CarePlanProposal) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    known = {c.id for c in packet.conditions} | {o.id for o in packet.observations}
    for index, activity in enumerate(proposal.activities):
        for ref in activity.reason_refs:
            if ref not in known:
                findings.append(
                    _finding(
                        "grounding",
                        f"activity-{index}-{ref}",
                        Severity.ERROR,
                        f"Activity {index} cites {ref!r}, which is not an active condition or "
                        "recent result in the patient's record",
                        activity_index=index,
                    )
                )
        med = activity.medication
        if activity.kind is ActivityKind.MEDICATION and med is None:
            findings.append(
                _finding(
                    "grounding",
                    f"activity-{index}-medication",
                    Severity.ERROR,
                    f"Activity {index} is a medication activity without medication details",
                    activity_index=index,
                )
            )
        if med is None:
            continue
        needs_existing = med.action is not MedicationAction.START
        if needs_existing and (
            med.existing_request_id is None or packet.medication(med.existing_request_id) is None
        ):
            findings.append(
                _finding(
                    "grounding",
                    f"activity-{index}-medication-target",
                    Severity.ERROR,
                    f"Activity {index} would {med.action.value} {med.display!r} but does not "
                    "reference one of the patient's active MedicationRequests",
                    activity_index=index,
                )
            )
    return findings


def check_duplicates_and_interactions(regimen: list[RegimenItem]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for first, second in combinations(regimen, 2):
        endorsed = first.endorsed or second.endorsed
        index = first.activity_index if first.activity_index is not None else second.activity_index
        subject = "|".join(sorted((first.key, second.key)))
        shared_ingredients = first.ingredients & second.ingredients
        shared_classes = first.classes & second.classes
        if shared_ingredients or shared_classes:
            what = (
                f"ingredient {sorted(i.name for i in shared_ingredients)[0]}"
                if shared_ingredients
                else f"class {sorted(c.value for c in shared_classes)[0]}"
            )
            intentional = bool(shared_classes) and shared_classes <= INTENTIONAL_COMBINATIONS
            severity = Severity.ERROR if endorsed and not intentional else Severity.WARNING
            findings.append(
                _finding(
                    "duplicate-therapy",
                    subject,
                    severity,
                    f"Duplicate therapy ({what}): {first.label!r} and {second.label!r}",
                    activity_index=index,
                    refs=(first.key, second.key),
                )
            )
        keys_a, keys_b = interaction_keys(first.ingredients), interaction_keys(second.ingredients)
        for rule in INTERACTIONS:
            a, b = str(rule.first), str(rule.second)
            if not ((a in keys_a and b in keys_b) or (b in keys_a and a in keys_b)):
                continue
            severity = rule.severity if endorsed else Severity.WARNING
            findings.append(
                _finding(
                    "interaction",
                    f"{rule.first}+{rule.second}:{subject}",
                    severity,
                    f"{rule.message}: {first.label!r} with {second.label!r}",
                    activity_index=index,
                    refs=(first.key, second.key),
                )
            )
    return findings


def check_allergies(packet: EvidencePacket, regimen: list[RegimenItem]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for allergy in packet.allergies:
        rules = allergen_rules(allergy.code)
        direct = ingredients_of(allergy.code)
        contraindicated = classes_of(direct).union(*(r.contraindicated for r in rules))
        cross = frozenset().union(*(r.cross_reactive for r in rules)) - contraindicated
        banned_ingredients = {i.rxcui for i in direct}.union(
            *(r.contraindicated_ingredients for r in rules)
        )
        for item in regimen:
            hit_ingredient = any(i.rxcui in banned_ingredients for i in item.ingredients)
            hit_class = bool(item.classes & contraindicated)
            subject = f"AllergyIntolerance/{allergy.id}:{item.key}"
            refs = (f"AllergyIntolerance/{allergy.id}", item.key)
            if hit_ingredient or hit_class:
                severity = Severity.ERROR if item.endorsed else Severity.WARNING
                state = "proposed" if item.endorsed else "active"
                findings.append(
                    _finding(
                        "allergy",
                        subject,
                        severity,
                        f"{state.capitalize()} medication {item.label!r} is contraindicated by "
                        f"documented allergy to {allergy.code.label!r}"
                        + (f" (criticality {allergy.criticality})" if allergy.criticality else ""),
                        activity_index=item.activity_index,
                        refs=refs,
                    )
                )
            elif item.classes & cross:
                findings.append(
                    _finding(
                        "allergy",
                        subject,
                        Severity.WARNING,
                        f"Medication {item.label!r} has potential cross-reactivity with "
                        f"documented allergy to {allergy.code.label!r}",
                        activity_index=item.activity_index,
                        refs=refs,
                    )
                )
    return findings


def check_follow_up(packet: EvidencePacket, proposal: CarePlanProposal) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    follow_ups = [(i, a) for i, a in enumerate(proposal.activities) if a.kind in FOLLOW_UP_KINDS]
    for condition in packet.conditions:
        rule = follow_up_rule(condition.code)
        if rule is None:
            continue
        covered = any(
            condition.id in a.reason_refs and a.due_in_days <= rule.max_days for _, a in follow_ups
        )
        if not covered:
            findings.append(
                _finding(
                    "missing-follow-up",
                    f"Condition/{condition.id}",
                    rule.severity,
                    f"No follow-up for {condition.code.label} ({rule.name}) within "
                    f"{rule.max_days} days",
                    refs=(f"Condition/{condition.id}",),
                )
            )
    encounter = packet.encounter
    if (
        encounter is not None
        and encounter.class_code in DISCHARGE_ENCOUNTER_CLASSES
        and encounter.status == "finished"
        and not any(
            a.kind is ActivityKind.FOLLOW_UP and a.due_in_days <= POST_DISCHARGE_FOLLOW_UP_DAYS
            for _, a in follow_ups
        )
    ):
        findings.append(
            _finding(
                "missing-follow-up",
                f"Encounter/{encounter.id}",
                Severity.ERROR,
                f"Discharge without a follow-up visit within {POST_DISCHARGE_FOLLOW_UP_DAYS} days",
                refs=(f"Encounter/{encounter.id}",),
            )
        )
    return findings


def check_labs(packet: EvidencePacket, proposal: CarePlanProposal) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    labs = [a for a in proposal.activities if a.kind is ActivityKind.LAB]
    for obs in packet.observations:
        codes = loinc_codes(obs.code)
        for rule in LAB_RULES:
            if not codes & rule.loinc or obs.value is None:
                continue
            too_low = rule.low is not None and obs.value < rule.low
            too_high = rule.high is not None and obs.value >= rule.high
            if not (too_low or too_high):
                continue
            rechecked = any(
                (a.code in rule.loinc or obs.id in a.reason_refs)
                and a.due_in_days <= rule.recheck_days
                for a in labs
            )
            if not rechecked:
                direction = "low" if too_low else "high"
                findings.append(
                    _finding(
                        "abnormal-lab",
                        f"Observation/{obs.id}",
                        rule.severity,
                        f"{rule.analyte} {obs.value:g} {obs.unit or ''} is {direction}; no recheck "
                        f"within {rule.recheck_days} days".replace("  ", " "),
                        refs=(f"Observation/{obs.id}",),
                    )
                )
    return findings


def latest_egfr(packet: EvidencePacket) -> ObservationFact | None:
    candidates = [
        o for o in packet.observations if loinc_codes(o.code) & EGFR and o.value is not None
    ]
    return max(candidates, key=lambda o: o.effective or "", default=None)


def check_renal_dosing(
    packet: EvidencePacket, regimen: list[RegimenItem]
) -> list[ValidationFinding]:
    egfr = latest_egfr(packet)
    if egfr is None or egfr.value is None:
        return []
    findings: list[ValidationFinding] = []
    for item in regimen:
        if DrugClass.BIGUANIDE not in item.classes:
            continue
        refs = (f"Observation/{egfr.id}", item.key)
        if egfr.value < METFORMIN_CONTRAINDICATED_EGFR:
            findings.append(
                _finding(
                    "renal-dosing",
                    item.key,
                    Severity.ERROR,
                    f"{item.label!r} is contraindicated at eGFR {egfr.value:g} "
                    f"(< {METFORMIN_CONTRAINDICATED_EGFR:g}); the plan must stop or replace it",
                    activity_index=item.activity_index,
                    refs=refs,
                )
            )
        elif egfr.value < METFORMIN_CAUTION_EGFR and item.key.startswith("proposal/"):
            findings.append(
                _finding(
                    "renal-dosing",
                    item.key,
                    Severity.WARNING,
                    f"Starting {item.label!r} at eGFR {egfr.value:g} "
                    f"(< {METFORMIN_CAUTION_EGFR:g}) is not recommended",
                    activity_index=item.activity_index,
                    refs=refs,
                )
            )
    return findings


def _ordered(findings: Iterable[ValidationFinding]) -> list[ValidationFinding]:
    unique: dict[str, ValidationFinding] = {}
    for finding in findings:
        unique.setdefault(finding.id, finding)
    rank = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return sorted(unique.values(), key=lambda f: (rank[f.severity], f.check, f.id))


def validate_plan(packet: EvidencePacket, proposal: CarePlanProposal) -> list[ValidationFinding]:
    regimen = build_regimen(packet, proposal)
    return _ordered(
        [
            *check_grounding(packet, proposal),
            *check_duplicates_and_interactions(regimen),
            *check_allergies(packet, regimen),
            *check_follow_up(packet, proposal),
            *check_labs(packet, proposal),
            *check_renal_dosing(packet, regimen),
        ]
    )


def validate_medications(
    packet: EvidencePacket, proposals: Sequence[MedicationProposal]
) -> list[ValidationFinding]:
    """Medication-only checks for proposed changes outside a care-plan workflow."""
    proposal = CarePlanProposal(
        title="Medication safety check",
        goals=("Medication safety review",),
        activities=tuple(
            ProposedActivity(
                kind=ActivityKind.MEDICATION,
                description=f"{p.action.value} {p.display}",
                due_in_days=0,
                medication=p,
            )
            for p in proposals
        ),
        rationale="Medication safety check of proposed changes requested by an agent.",
    )
    regimen = build_regimen(packet, proposal)
    return _ordered(
        [
            *check_grounding(packet, proposal),
            *check_duplicates_and_interactions(regimen),
            *check_allergies(packet, regimen),
            *check_renal_dosing(packet, regimen),
        ]
    )
