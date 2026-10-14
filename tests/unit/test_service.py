"""Use cases: sign-off decisions, revisions, resume, cancel, idempotency, medication checks."""

from __future__ import annotations

import copy

import pytest

from clinical_workflow_orchestration.domain.careplan import MedicationAction, MedicationProposal
from clinical_workflow_orchestration.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    OrchestrationError,
)
from clinical_workflow_orchestration.domain.workflow import (
    Acknowledgement,
    SignOffDecision,
    WorkflowState,
)
from clinical_workflow_orchestration.runtime.container import Container
from clinical_workflow_orchestration.security.users import UserPrincipal
from tests.conftest import RISKY_DRAFT, FakeFhirServer, FakeModel
from tests.helpers import CLINICIAN, approve, run_until_idle, start_and_wait


async def test_start_is_idempotent_per_key(container: Container) -> None:
    first, created = await container.service.start(
        encounter_id="cwo-e1", idempotency_key="discharge-cwo-e1", actor="a"
    )
    again, created_again = await container.service.start(
        encounter_id="cwo-e1", idempotency_key="discharge-cwo-e1", actor="a"
    )
    assert created
    assert not created_again
    assert again.id == first.id
    with pytest.raises(ConflictError):
        await container.service.start(
            encounter_id="cwo-e2", idempotency_key="discharge-cwo-e1", actor="a"
        )
    await run_until_idle(container)


async def test_get_unknown_workflow(container: Container) -> None:
    with pytest.raises(NotFoundError):
        await container.service.get("0" * 32)


async def test_reject_is_terminal(container: Container, fhir: FakeFhirServer) -> None:
    workflow = await start_and_wait(container)
    assert workflow.draft is not None
    rejected = await container.service.decide(
        workflow.id,
        principal=CLINICIAN,
        decision=SignOffDecision.REJECT,
        draft_digest=workflow.draft.digest,
        comment="Not appropriate for this patient",
        acknowledgements=(),
    )
    assert rejected.state is WorkflowState.REJECTED
    assert rejected.signoff is not None
    assert rejected.signoff.signature is None
    assert fhir.transactions == []


async def test_request_changes_redrafts_with_feedback(
    container: Container, model: FakeModel
) -> None:
    workflow = await start_and_wait(container)
    assert workflow.draft is not None
    model.draft["activities"].append(
        {"kind": "referral", "description": "Dietitian referral", "due_in_days": 14}
    )
    returned = await container.service.decide(
        workflow.id,
        principal=CLINICIAN,
        decision=SignOffDecision.REQUEST_CHANGES,
        draft_digest=workflow.draft.digest,
        comment="Add a dietitian referral",
        acknowledgements=(),
    )
    assert returned.state is WorkflowState.DRAFTING
    await run_until_idle(container)
    redrafted = await container.service.get(workflow.id)
    assert redrafted.state is WorkflowState.AWAITING_APPROVAL
    assert redrafted.revision == 1
    assert redrafted.draft is not None
    assert redrafted.draft.revision == 1
    assert redrafted.draft.digest != workflow.draft.digest
    draft_prompt = model.calls[-2][1]["messages"][-1]["content"]
    assert "Add a dietitian referral" in draft_prompt


async def test_request_changes_needs_comment_and_has_a_limit(container: Container) -> None:
    workflow = await start_and_wait(container)
    assert workflow.draft is not None
    with pytest.raises(OrchestrationError, match="comment"):
        await container.service.decide(
            workflow.id,
            principal=CLINICIAN,
            decision=SignOffDecision.REQUEST_CHANGES,
            draft_digest=workflow.draft.digest,
            comment=None,
            acknowledgements=(),
        )
    container.service._max_revisions = 0  # type: ignore[attr-defined]
    with pytest.raises(InvalidStateError, match="revision limit"):
        await container.service.decide(
            workflow.id,
            principal=CLINICIAN,
            decision=SignOffDecision.REQUEST_CHANGES,
            draft_digest=workflow.draft.digest,
            comment="More detail please",
            acknowledgements=(),
        )


async def test_stale_digest_is_refused(container: Container) -> None:
    workflow = await start_and_wait(container)
    with pytest.raises(ConflictError, match="changed since it was displayed"):
        await container.service.decide(
            workflow.id,
            principal=CLINICIAN,
            decision=SignOffDecision.APPROVE,
            draft_digest="sha256:" + "0" * 64,
            comment=None,
            acknowledgements=(),
        )


async def test_errors_must_be_acknowledged(
    container: Container, model: FakeModel, fhir: FakeFhirServer
) -> None:
    model.draft = copy.deepcopy(RISKY_DRAFT)
    workflow = await start_and_wait(container)
    assert workflow.validation is not None
    checks = {f.check for f in workflow.validation.errors}
    assert {"allergy", "interaction", "missing-follow-up"} <= checks
    with pytest.raises(ForbiddenError, match="deny-approval-with-unacknowledged-errors"):
        await approve(container, workflow, acknowledge_errors=False)
    assert fhir.transactions == []
    done = await approve(container, workflow, acknowledge_errors=True)
    assert done.state is WorkflowState.COMPLETED
    assert done.signoff is not None
    assert {a.finding_id for a in done.signoff.acknowledgements} == {
        f.id for f in workflow.validation.errors
    }


async def test_unknown_acknowledgement_is_rejected(container: Container) -> None:
    workflow = await start_and_wait(container)
    assert workflow.draft is not None
    with pytest.raises(OrchestrationError, match="unknown findings"):
        await container.service.decide(
            workflow.id,
            principal=CLINICIAN,
            decision=SignOffDecision.APPROVE,
            draft_digest=workflow.draft.digest,
            comment=None,
            acknowledgements=(
                Acknowledgement(finding_id="allergy:none", reason="not a real finding id"),
            ),
        )


async def test_signer_needs_role_and_practitioner(container: Container) -> None:
    workflow = await start_and_wait(container)
    no_role = UserPrincipal(
        subject="x@hospital.test",
        roles=frozenset({"care-coordinator"}),
        fhir_user="Practitioner/9",
        token_id=None,
    )
    no_fhir_user = CLINICIAN.model_copy(update={"fhir_user": None})
    for principal, rule in (
        (no_role, "default deny"),
        (no_fhir_user, "deny-signoff-without-practitioner"),
    ):
        with pytest.raises(ForbiddenError, match=rule):
            await approve(container, workflow, principal=principal)


async def test_decision_requires_awaiting_state(container: Container) -> None:
    workflow = await start_and_wait(container)
    done = await approve(container, workflow)
    with pytest.raises(AssertionError):
        await approve(container, done)
    assert done.draft is not None
    with pytest.raises(InvalidStateError):
        await container.service.decide(
            done.id,
            principal=CLINICIAN,
            decision=SignOffDecision.REJECT,
            draft_digest=done.draft.digest,
            comment=None,
            acknowledgements=(),
        )


async def test_resume_and_cancel(container: Container, fhir: FakeFhirServer) -> None:
    fhir.fail_with = [400]
    failed = await start_and_wait(container)
    assert failed.state is WorkflowState.FAILED
    resumed = await container.service.resume(failed.id, actor="casey")
    assert resumed.state is WorkflowState.RETRIEVING_EVIDENCE
    await run_until_idle(container)
    waiting = await container.service.get(failed.id)
    assert waiting.state is WorkflowState.AWAITING_APPROVAL
    with pytest.raises(InvalidStateError):
        await container.service.resume(waiting.id, actor="casey")
    cancelled = await container.service.cancel(
        waiting.id, actor="casey", reason="patient transferred"
    )
    assert cancelled.state is WorkflowState.CANCELLED
    with pytest.raises(InvalidStateError):
        await container.service.cancel(waiting.id, actor="casey", reason="again please")


async def test_list_by_state(container: Container) -> None:
    workflow = await start_and_wait(container)
    waiting = await container.service.list_workflows(
        state=WorkflowState.AWAITING_APPROVAL, limit=10, offset=0
    )
    assert [w.id for w in waiting] == [workflow.id]
    completed = await container.service.list_workflows(
        state=WorkflowState.COMPLETED, limit=10, offset=0
    )
    assert completed == []
    assert len(await container.service.list_workflows(state=None, limit=10, offset=0)) == 1


async def test_medication_check(container: Container) -> None:
    findings = await container.service.check_medications(
        patient_id="cwo-p1",
        proposals=[
            MedicationProposal(
                action=MedicationAction.START, display="amoxicillin 500 MG Oral Capsule"
            ),
            MedicationProposal(action=MedicationAction.START, display="enalapril 5 MG Oral Tablet"),
        ],
        actor="agent",
    )
    checks = {(f.check, f.severity.value) for f in findings}
    assert ("allergy", "error") in checks
    assert ("duplicate-therapy", "error") in checks
