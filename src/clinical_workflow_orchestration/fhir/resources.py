"""FHIR R4 datatypes and lenient accessors for the resources this service reads and writes.

FHIR servers return large, extensible JSON. The orchestrator reads a narrow,
well-defined subset of each resource, so it parses into small typed value
objects instead of carrying the full resource model around; resources it
writes are built in :mod:`clinical_workflow_orchestration.fhir.builders`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict

JsonObject = dict[str, Any]

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
UCUM = "http://unitsofmeasure.org"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_DATA_OPERATION = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"
PROVENANCE_PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
TASK_CODE = "http://hl7.org/fhir/CodeSystem/task-code"
SIGNATURE_TYPE = "urn:iso-astm:E1762-95:2013"
VERIFICATION_SIGNATURE = "1.2.840.10065.1.12.1.5"
CONDITION_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
ALLERGY_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
ALLERGY_VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"

# FHIR R4 id datatype: [A-Za-z0-9\-\.]{1,64}
_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def validate_fhir_id(value: str) -> str:
    """Reject anything that is not a FHIR logical id (prevents path and query injection)."""
    if not _FHIR_ID.fullmatch(value):
        raise ValueError(f"invalid FHIR id: {value!r}")
    return value


FhirId = Annotated[str, AfterValidator(validate_fhir_id)]


class Coding(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str | None = None
    code: str | None = None
    display: str | None = None


class CodeableConcept(BaseModel):
    model_config = ConfigDict(frozen=True)

    coding: tuple[Coding, ...] = ()
    text: str | None = None

    def codes(self, system: str) -> tuple[str, ...]:
        return tuple(c.code for c in self.coding if c.system == system and c.code)

    def has_code(self, system: str, code: str) -> bool:
        return code in self.codes(system)

    @property
    def label(self) -> str:
        if self.text:
            return self.text
        for coding in self.coding:
            if coding.display:
                return coding.display
        for coding in self.coding:
            if coding.code:
                return coding.code
        return "unspecified"

    def as_fhir(self) -> JsonObject:
        out: JsonObject = {}
        if self.coding:
            out["coding"] = [c.model_dump(exclude_none=True) for c in self.coding]
        if self.text:
            out["text"] = self.text
        return out


def parse_codeable_concept(value: Any) -> CodeableConcept:
    """Lenient parse: malformed codings are skipped rather than failing the whole resource."""
    if not isinstance(value, dict):
        return CodeableConcept()
    codings: list[Coding] = []
    for raw in value.get("coding") or []:
        if isinstance(raw, dict):
            codings.append(
                Coding(
                    system=_str(raw.get("system")),
                    code=_str(raw.get("code")),
                    display=_str(raw.get("display")),
                )
            )
    return CodeableConcept(coding=tuple(codings), text=_str(value.get("text")))


def first_codeable_concept(value: Any) -> CodeableConcept:
    """For 0..* CodeableConcept elements (e.g. ``Encounter.type``) take the first one."""
    if isinstance(value, list) and value:
        return parse_codeable_concept(value[0])
    return parse_codeable_concept(value)


def status_code(value: Any) -> str | None:
    """Code of a status CodeableConcept such as ``Condition.clinicalStatus``."""
    concept = parse_codeable_concept(value)
    for coding in concept.coding:
        if coding.code:
            return coding.code
    return concept.text


def reference_id(reference: Any, resource_type: str) -> str | None:
    """Logical id from a Reference to ``resource_type`` (relative or absolute URL)."""
    if isinstance(reference, dict):
        reference = reference.get("reference")
    if not isinstance(reference, str):
        return None
    parts = reference.rstrip("/").split("/")
    for index in range(len(parts) - 1):
        if parts[index] == resource_type and _FHIR_ID.fullmatch(parts[index + 1]):
            return parts[index + 1]
    return None


def location_reference(location: str) -> str:
    """``CarePlan/12/_history/1`` (or an absolute URL) -> ``CarePlan/12``."""
    parts = location.split("/_history/", maxsplit=1)[0].rstrip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"unexpected FHIR location {location!r}")
    return f"{parts[-2]}/{parts[-1]}"


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_str(value: Any) -> str | None:
    return _str(value)


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
