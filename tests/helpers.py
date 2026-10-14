"""Helpers shared by test suites: drive workflows through the service and engine."""

from __future__ import annotations

from clinical_workflow_orchestration.domain.workflow import (
    Acknowledgement,
    SignOffDecision,
    Workflow,
    WorkflowState,
)
from clinical_workflow_orchestration.runtime.container import Container
from clinical_workflow_orchestration.security.users import UserPrincipal
from tests.conftest import PRACTITIONER

CLINICIAN = UserPrincipal(
    subject="avery.synthetic@hospital.test",
    roles=frozenset({"clinician-approver"}),
    fhir_user=PRACTITIONER,
    token_id="jti-clinician-1",
)
COORDINATOR = UserPrincipal(
    subject="casey.coordinator@hospital.test",
    roles=frozenset({"care-coordinator"}),
    fhir_user=None,
    token_id=None,
)


async def run_until_idle(container: Container) -> None:
    await container.engine_runner.drain()


async def start_and_wait(container: Container, encounter_id: str = "cwo-e1") -> Workflow:
    workflow, _ = await container.service.start(
        encounter_id=encounter_id, idempotency_key=None, actor=COORDINATOR.subject
    )
    await run_until_idle(container)
    return await container.service.get(workflow.id)


async def approve(
    container: Container,
    workflow: Workflow,
    *,
    acknowledge_errors: bool = True,
    principal: UserPrincipal = CLINICIAN,
) -> Workflow:
    assert workflow.state is WorkflowState.AWAITING_APPROVAL, workflow.failure
    assert workflow.draft is not None
    assert workflow.validation is not None
    acks = (
        tuple(
            Acknowledgement(
                finding_id=f.id, reason="Reviewed with the patient; benefit outweighs risk"
            )
            for f in workflow.validation.errors
        )
        if acknowledge_errors
        else ()
    )
    decided = await container.service.decide(
        workflow.id,
        principal=principal,
        decision=SignOffDecision.APPROVE,
        draft_digest=workflow.draft.digest,
        comment="Agree with plan",
        acknowledgements=acks,
    )
    await run_until_idle(container)
    return await container.service.get(decided.id)
