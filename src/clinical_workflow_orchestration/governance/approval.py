"""Policy decisions for people and for EHR writes, recorded on the workflow.

All governance questions are asked of the same versioned policy set
(``policies/clinical-workflow.yaml``) through the agentic-core PolicyEngine,
so every decision carries the policy version and digest that produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentic_core.domain.classification import DataClassification, RiskLevel
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject

from clinical_workflow_orchestration.domain.careplan import ValidationReport
from clinical_workflow_orchestration.domain.workflow import (
    Acknowledgement,
    PolicyRecord,
    Workflow,
)
from clinical_workflow_orchestration.security.users import UserPrincipal

EHR_WRITE = "fhir:write:careplan-bundle"


def unacknowledged_errors(
    report: ValidationReport | None, acknowledgements: tuple[Acknowledgement, ...]
) -> list[str]:
    if report is None:
        return []
    acknowledged = {a.finding_id for a in acknowledgements}
    return [f.id for f in report.errors if f.id not in acknowledged]


def user_subject(principal: UserPrincipal) -> Subject:
    attributes: dict[str, Any] = {}
    if principal.fhir_user:
        attributes["fhir_user"] = principal.fhir_user
    return Subject(id=principal.subject, kind="user", roles=principal.roles, attributes=attributes)


class Governance:
    def __init__(self, engine: PolicyEngine, *, orchestrator_id: str) -> None:
        self._engine = engine
        self._orchestrator_id = orchestrator_id

    @property
    def engine(self) -> PolicyEngine:
        return self._engine

    def evaluate(
        self,
        *,
        subject: Subject,
        action: str,
        resource: Resource,
        at: datetime,
        environment: dict[str, Any] | None = None,
        risk: RiskLevel = RiskLevel.MODERATE,
    ) -> PolicyRecord:
        decision = self._engine.evaluate(
            PolicyRequest(
                subject=subject,
                action=action,
                resource=resource,
                risk=risk,
                environment=environment or {},
            )
        )
        return PolicyRecord(
            action=action,
            decision=decision.decision,
            matched_rules=decision.matched_rules,
            policy_name=decision.policy_name,
            policy_version=decision.policy_version,
            policy_digest=decision.policy_digest,
            reason=decision.reason,
            decided_at=at,
        )

    def user_action(
        self,
        principal: UserPrincipal,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        at: datetime,
        purpose_of_use: str = "TREAT",
    ) -> PolicyRecord:
        return self.evaluate(
            subject=user_subject(principal),
            action=action,
            resource=Resource(
                type=resource_type, id=resource_id, classification=DataClassification.RESTRICTED
            ),
            environment={"purpose_of_use": purpose_of_use},
            at=at,
        )

    def ehr_write(
        self,
        workflow: Workflow,
        *,
        at: datetime,
        acknowledgements: tuple[Acknowledgement, ...] = (),
    ) -> PolicyRecord:
        """May the orchestrator write this workflow's plan to the EHR now?"""
        signoff = workflow.signoff
        acks = signoff.acknowledgements if signoff is not None else acknowledgements
        environment: dict[str, Any] = {
            "clinician_signed": signoff is not None and signoff.signature is not None,
            "purpose_of_use": "TREAT",
        }
        if signoff is not None:
            environment["signer"] = signoff.practitioner
        return self.evaluate(
            subject=Subject(id=self._orchestrator_id, kind="service"),
            action=EHR_WRITE,
            resource=Resource(
                type="CarePlan",
                id=workflow.id,
                classification=DataClassification.RESTRICTED,
                attributes={
                    "unacknowledged_errors": len(unacknowledged_errors(workflow.validation, acks)),
                    "medication_changes": sum(
                        1
                        for a in (workflow.draft.proposal.activities if workflow.draft else ())
                        if a.medication is not None
                    ),
                },
            ),
            environment=environment,
            risk=RiskLevel.HIGH,
            at=at,
        )

    def signoff(
        self,
        principal: UserPrincipal,
        workflow: Workflow,
        *,
        action: str,
        acknowledgements: tuple[Acknowledgement, ...],
        at: datetime,
    ) -> PolicyRecord:
        return self.evaluate(
            subject=user_subject(principal),
            action=action,
            resource=Resource(
                type="CarePlan",
                id=workflow.id,
                classification=DataClassification.RESTRICTED,
                attributes={
                    "drafted_by": workflow.draft.drafted_by if workflow.draft else "",
                    "unacknowledged_errors": len(
                        unacknowledged_errors(workflow.validation, acknowledgements)
                    ),
                },
            ),
            environment={"purpose_of_use": "TREAT"},
            risk=RiskLevel.HIGH,
            at=at,
        )
