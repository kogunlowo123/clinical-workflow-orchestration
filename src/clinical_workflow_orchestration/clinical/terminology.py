"""Clinical knowledge tables used by the deterministic validation rules.

The tables are deliberately small, explicit and versioned: every entry is a
reviewable fact (RxNorm ingredient -> therapeutic class, allergen ->
contraindicated classes, condition -> follow-up interval, lab analyte ->
action threshold). Deployments extend them through change control; the
ruleset version is recorded with every validation report so a reviewer can
tell which knowledge produced which finding.

Sources for the facts encoded here: RxNorm ingredient concepts (NLM),
SNOMED CT and ICD-10-CM condition concepts, LOINC analyte codes. Clinical
thresholds follow widely used practice guidance (for example early
post-discharge follow-up for heart failure, metformin contraindication at
eGFR below 30 mL/min/1.73m2); local medical leadership owns their values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clinical_workflow_orchestration.fhir.resources import (
    ICD10,
    ICD10CM,
    LOINC,
    RXNORM,
    SNOMED,
    CodeableConcept,
)

RULESET_VERSION = "2026.10.1"


class DrugClass(StrEnum):
    ACE_INHIBITOR = "ace-inhibitor"
    ARB = "angiotensin-receptor-blocker"
    STATIN = "statin"
    BIGUANIDE = "biguanide"
    ANTICOAGULANT = "anticoagulant"
    NSAID = "nsaid"
    ANTIPLATELET = "antiplatelet"
    SSRI = "ssri"
    PPI = "proton-pump-inhibitor"
    PENICILLIN = "penicillin"
    CEPHALOSPORIN = "cephalosporin"
    SULFONAMIDE_ANTIBIOTIC = "sulfonamide-antibiotic"
    LOOP_DIURETIC = "loop-diuretic"
    THIAZIDE_DIURETIC = "thiazide-diuretic"
    BETA_BLOCKER = "beta-blocker"
    CALCIUM_CHANNEL_BLOCKER = "calcium-channel-blocker"
    BASAL_INSULIN = "basal-insulin"
    THYROID_HORMONE = "thyroid-hormone"
    SYSTEMIC_CORTICOSTEROID = "systemic-corticosteroid"
    MINERALOCORTICOID_ANTAGONIST = "mineralocorticoid-antagonist"


@dataclass(frozen=True)
class Ingredient:
    rxcui: str
    name: str
    classes: frozenset[DrugClass]


def _ing(rxcui: str, name: str, *classes: DrugClass) -> Ingredient:
    return Ingredient(rxcui=rxcui, name=name, classes=frozenset(classes))


INGREDIENTS: tuple[Ingredient, ...] = (
    _ing("29046", "lisinopril", DrugClass.ACE_INHIBITOR),
    _ing("3827", "enalapril", DrugClass.ACE_INHIBITOR),
    _ing("35296", "ramipril", DrugClass.ACE_INHIBITOR),
    _ing("52175", "losartan", DrugClass.ARB),
    _ing("69749", "valsartan", DrugClass.ARB),
    _ing("83367", "atorvastatin", DrugClass.STATIN),
    _ing("36567", "simvastatin", DrugClass.STATIN),
    _ing("301542", "rosuvastatin", DrugClass.STATIN),
    _ing("6809", "metformin", DrugClass.BIGUANIDE),
    _ing("11289", "warfarin", DrugClass.ANTICOAGULANT),
    _ing("1364430", "apixaban", DrugClass.ANTICOAGULANT),
    _ing("1114195", "rivaroxaban", DrugClass.ANTICOAGULANT),
    _ing("5640", "ibuprofen", DrugClass.NSAID),
    _ing("7258", "naproxen", DrugClass.NSAID),
    _ing("1191", "aspirin", DrugClass.ANTIPLATELET),
    _ing("32968", "clopidogrel", DrugClass.ANTIPLATELET),
    _ing("36437", "sertraline", DrugClass.SSRI),
    _ing("4493", "fluoxetine", DrugClass.SSRI),
    _ing("7646", "omeprazole", DrugClass.PPI),
    _ing("40790", "pantoprazole", DrugClass.PPI),
    _ing("723", "amoxicillin", DrugClass.PENICILLIN),
    _ing("7980", "penicillin g", DrugClass.PENICILLIN),
    _ing("2231", "cephalexin", DrugClass.CEPHALOSPORIN),
    _ing("10180", "sulfamethoxazole", DrugClass.SULFONAMIDE_ANTIBIOTIC),
    _ing("4603", "furosemide", DrugClass.LOOP_DIURETIC),
    _ing("5487", "hydrochlorothiazide", DrugClass.THIAZIDE_DIURETIC),
    _ing("6918", "metoprolol", DrugClass.BETA_BLOCKER),
    _ing("20352", "carvedilol", DrugClass.BETA_BLOCKER),
    _ing("17767", "amlodipine", DrugClass.CALCIUM_CHANNEL_BLOCKER),
    _ing("274783", "insulin glargine", DrugClass.BASAL_INSULIN),
    _ing("10582", "levothyroxine", DrugClass.THYROID_HORMONE),
    _ing("8640", "prednisone", DrugClass.SYSTEMIC_CORTICOSTEROID),
    _ing("9997", "spironolactone", DrugClass.MINERALOCORTICOID_ANTAGONIST),
)

_BY_RXCUI = {i.rxcui: i for i in INGREDIENTS}
_NAME_PATTERNS = tuple(
    (re.compile(rf"\b{re.escape(i.name)}\b", re.IGNORECASE), i) for i in INGREDIENTS
)


def ingredients_of(
    concept: CodeableConcept, extra_text: str | None = None
) -> frozenset[Ingredient]:
    """Ingredients identified by RxNorm code, else by ingredient name in display text.

    Clinical drug concepts (SCD/SBD) rarely carry the ingredient code, so the
    name match covers ``"lisinopril 10 MG Oral Tablet"``; matching is on word
    boundaries to avoid substring false positives.
    """
    found = {_BY_RXCUI[c] for c in concept.codes(RXNORM) if c in _BY_RXCUI}
    texts = [concept.text or "", extra_text or ""]
    texts.extend(c.display or "" for c in concept.coding)
    haystack = " ".join(t for t in texts if t)
    if haystack:
        found.update(i for pattern, i in _NAME_PATTERNS if pattern.search(haystack))
    return frozenset(found)


def classes_of(ingredients: frozenset[Ingredient]) -> frozenset[DrugClass]:
    return frozenset(c for i in ingredients for c in i.classes)


# ----------------------------------------------------------------- interactions


class Severity(StrEnum):
    ERROR = "error"  # must be resolved or explicitly acknowledged by the signing clinician
    WARNING = "warning"  # shown to the clinician; does not block sign-off
    INFO = "info"


@dataclass(frozen=True)
class Interaction:
    first: str  # DrugClass value or "rx:<rxcui>"
    second: str
    severity: Severity
    message: str


INTERACTIONS: tuple[Interaction, ...] = (
    Interaction(
        DrugClass.ACE_INHIBITOR,
        DrugClass.ARB,
        Severity.ERROR,
        "Dual renin-angiotensin blockade (ACE inhibitor with ARB): hyperkalaemia and acute "
        "kidney injury risk",
    ),
    Interaction(
        DrugClass.ANTICOAGULANT,
        DrugClass.NSAID,
        Severity.ERROR,
        "Anticoagulant with NSAID: major bleeding risk",
    ),
    Interaction(
        DrugClass.ANTICOAGULANT,
        DrugClass.ANTIPLATELET,
        Severity.WARNING,
        "Anticoagulant with antiplatelet: increased bleeding risk; confirm indication",
    ),
    Interaction(
        DrugClass.ACE_INHIBITOR,
        DrugClass.MINERALOCORTICOID_ANTAGONIST,
        Severity.WARNING,
        "ACE inhibitor with mineralocorticoid antagonist: monitor potassium",
    ),
    Interaction(
        DrugClass.ARB,
        DrugClass.MINERALOCORTICOID_ANTAGONIST,
        Severity.WARNING,
        "ARB with mineralocorticoid antagonist: monitor potassium",
    ),
    Interaction(
        "rx:32968",
        "rx:7646",
        Severity.WARNING,
        "Clopidogrel with omeprazole: CYP2C19 inhibition may reduce antiplatelet effect",
    ),
)


def interaction_keys(ingredients: frozenset[Ingredient]) -> frozenset[str]:
    return frozenset(
        {str(c) for c in classes_of(ingredients)} | {f"rx:{i.rxcui}" for i in ingredients}
    )


# -------------------------------------------------------------------- allergies


@dataclass(frozen=True)
class AllergenRule:
    name: str
    snomed: frozenset[str]
    text_terms: tuple[str, ...]
    contraindicated: frozenset[DrugClass]
    cross_reactive: frozenset[DrugClass]
    contraindicated_ingredients: frozenset[str] = frozenset()


ALLERGENS: tuple[AllergenRule, ...] = (
    AllergenRule(
        name="penicillins",
        snomed=frozenset({"91936005", "764146007"}),
        text_terms=("penicillin", "amoxicillin"),
        contraindicated=frozenset({DrugClass.PENICILLIN}),
        cross_reactive=frozenset({DrugClass.CEPHALOSPORIN}),
    ),
    AllergenRule(
        name="sulfonamide antibiotics",
        snomed=frozenset({"91939003"}),
        text_terms=("sulfonamide", "sulfa", "sulfamethoxazole"),
        contraindicated=frozenset({DrugClass.SULFONAMIDE_ANTIBIOTIC}),
        cross_reactive=frozenset(),
    ),
    AllergenRule(
        name="aspirin and NSAIDs",
        snomed=frozenset({"293586001"}),
        text_terms=("aspirin", "nsaid", "ibuprofen", "naproxen"),
        contraindicated=frozenset({DrugClass.NSAID}),
        cross_reactive=frozenset(),
        contraindicated_ingredients=frozenset({"1191"}),
    ),
    AllergenRule(
        name="ACE inhibitors",
        snomed=frozenset(),
        text_terms=("ace inhibitor", "lisinopril", "enalapril", "ramipril"),
        contraindicated=frozenset({DrugClass.ACE_INHIBITOR}),
        cross_reactive=frozenset({DrugClass.ARB}),
    ),
)


def allergen_rules(concept: CodeableConcept) -> tuple[AllergenRule, ...]:
    """Allergen rules triggered by an AllergyIntolerance.code (SNOMED, RxNorm or text)."""
    matched: list[AllergenRule] = []
    snomed = set(concept.codes(SNOMED))
    text = " ".join(
        t.lower() for t in [concept.text or "", *(c.display or "" for c in concept.coding)] if t
    )
    coded_classes = classes_of(ingredients_of(concept))
    for rule in ALLERGENS:
        by_code = bool(snomed & rule.snomed)
        by_text = any(re.search(rf"\b{re.escape(term)}\b", text) for term in rule.text_terms)
        by_class = bool(coded_classes & rule.contraindicated)
        if by_code or by_text or by_class:
            matched.append(rule)
    return tuple(matched)


# ------------------------------------------------------------------- follow-up


@dataclass(frozen=True)
class FollowUpRule:
    name: str
    snomed: frozenset[str]
    icd10_prefixes: tuple[str, ...]
    max_days: int
    severity: Severity


FOLLOW_UP_RULES: tuple[FollowUpRule, ...] = (
    FollowUpRule("heart failure", frozenset({"84114007"}), ("I50",), 7, Severity.ERROR),
    FollowUpRule(
        "chronic obstructive pulmonary disease",
        frozenset({"13645005"}),
        ("J44",),
        14,
        Severity.ERROR,
    ),
    FollowUpRule(
        "type 2 diabetes mellitus", frozenset({"44054006"}), ("E11",), 30, Severity.WARNING
    ),
    FollowUpRule("hypertension", frozenset({"38341003"}), ("I10",), 30, Severity.WARNING),
    FollowUpRule(
        "chronic kidney disease", frozenset({"709044004"}), ("N18",), 30, Severity.WARNING
    ),
)

POST_DISCHARGE_FOLLOW_UP_DAYS = 14
DISCHARGE_ENCOUNTER_CLASSES = frozenset({"IMP", "ACUTE", "NONAC", "EMER", "OBSENC"})


def follow_up_rule(concept: CodeableConcept) -> FollowUpRule | None:
    snomed = set(concept.codes(SNOMED))
    icd = concept.codes(ICD10CM) + concept.codes(ICD10)
    for rule in FOLLOW_UP_RULES:
        if snomed & rule.snomed:
            return rule
        if any(code.replace(".", "").upper().startswith(rule.icd10_prefixes) for code in icd):
            return rule
    return None


# ------------------------------------------------------------------------- labs


@dataclass(frozen=True)
class LabRule:
    analyte: str
    loinc: frozenset[str]
    low: float | None
    high: float | None
    recheck_days: int
    severity: Severity


POTASSIUM = frozenset({"2823-3", "6298-4"})
HBA1C = frozenset({"4548-4", "17856-6"})
EGFR = frozenset({"33914-3", "62238-1", "98979-8", "48642-3", "48643-1"})
INR = frozenset({"6301-6", "34714-6"})

LAB_RULES: tuple[LabRule, ...] = (
    LabRule("potassium", POTASSIUM, low=3.0, high=5.5, recheck_days=3, severity=Severity.ERROR),
    LabRule("HbA1c", HBA1C, low=None, high=9.0, recheck_days=30, severity=Severity.WARNING),
    LabRule("INR", INR, low=None, high=4.0, recheck_days=3, severity=Severity.ERROR),
)

METFORMIN_CONTRAINDICATED_EGFR = 30.0
METFORMIN_CAUTION_EGFR = 45.0


def loinc_codes(concept: CodeableConcept) -> frozenset[str]:
    return frozenset(concept.codes(LOINC))
