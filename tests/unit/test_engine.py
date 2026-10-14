"""Workflow engine: steps, persistence of outputs, failures and automatic retries."""

from __future__ import annotations

from datetime import timedelta

from clinical_workflow_orchestration.domain.careplan import ValidationOutcome
from clinical_workflow_orchestration.domain.workflow import WorkflowState
from clinical_workflow_orchestration.orchestration.engine import classify_failure
from clinical_workflow_orchestration.runtime.container import Container
from tests.conftest import FakeClock, FakeFhirServer, FakeModel
from tests.helpers import approve, run_until_idle, start_and_wait


async def test_runs_to_awaiting_approval(container: Container, model: FakeModel) -> None:
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.AWAITING_APPROVAL, workflow.failure
    assert workflow.patient_id == "cwo-p1"
    assert workflow.evidence is not None
    assert workflow.draft is not None
    assert workflow.validation is not None
    assert workflow.validation.outcome is ValidationOutcome.NEEDS_ATTENTION
    assert workflow.approval is not None
    assert workflow.approval.decision.value == "require_approval"
    assert "approve-unsigned-ehr-writes" in workflow.approval.matched_rules
    assert [t.to_state for t in workflow.history] == [
        WorkflowState.RETRIEVING_EVIDENCE,
        WorkflowState.DRAFTING,
        WorkflowState.VALIDATING,
        WorkflowState.AWAITING_APPROVAL,
    ]
    assert len(model.calls) == 2


async def test_approval_commits_plan(container: Container, fhir: FakeFhirServer) -> None:
    workflow = await start_and_wait(container)
    done = await approve(container, workflow)
    assert done.state is WorkflowState.COMPLETED, done.failure
    assert done.commit is not None
    assert done.commit.care_plan.startswith("CarePlan/")
    assert len(done.commit.tasks) == 6
    assert done.commit.provenance.startswith("Provenance/")
    assert len(fhir.transactions) == 1


async def test_missing_encounter_fails_without_retry(container: Container) -> None:
    workflow = await start_and_wait(container, encounter_id="does-not-exist")
    assert workflow.state is WorkflowState.FAILED
    assert workflow.failure is not None
    assert workflow.failure.step is WorkflowState.RETRIEVING_EVIDENCE
    assert workflow.failure.code == "fhir_not_found"
    assert workflow.failure.retryable is False
    assert workflow.failure.next_retry_at is None


async def test_transient_model_outage_retries_automatically(
    container: Container, model: FakeModel, clock: FakeClock
) -> None:
    model.fail_all = True
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.FAILED
    assert workflow.failure is not None
    assert workflow.failure.retryable
    assert workflow.failure.step is WorkflowState.DRAFTING
    assert workflow.failure.next_retry_at is not None

    assert await container.engine_runner.sweep_once() == 0  # not yet due
    model.fail_all = False
    clock.advance(31)
    assert await container.engine_runner.sweep_once() == 1
    await run_until_idle(container)
    recovered = await container.service.get(workflow.id)
    assert recovered.state is WorkflowState.AWAITING_APPROVAL
    assert recovered.failure is None
    assert any(t.reason.startswith("automatic retry 1") for t in recovered.history)


async def test_retry_budget_is_bounded(
    container: Container, model: FakeModel, clock: FakeClock
) -> None:
    model.fail_all = True
    workflow = await start_and_wait(container)
    for _ in range(3):
        clock.advance(10_000)
        await container.engine_runner.sweep_once()
        await run_until_idle(container)
    exhausted = await container.service.get(workflow.id)
    assert exhausted.state is WorkflowState.FAILED
    assert exhausted.failure is not None
    assert exhausted.failure.attempts == 4
    assert exhausted.failure.next_retry_at is None  # waits for a person now
    clock.advance(10_000)
    assert await container.engine_runner.sweep_once() == 0


async def test_lease_prevents_concurrent_advancement(container: Container) -> None:
    workflow, _ = await container.service.start(
        encounter_id="cwo-e1", idempotency_key="lease-test-key", actor="tester"
    )
    await run_until_idle(container)
    repo = container.service._repo  # type: ignore[attr-defined]
    now = container.clock.now()
    assert await repo.acquire_lease(
        workflow.id, owner="other-replica", now=now, until=now + timedelta(minutes=5)
    )
    engine = container.service._engine  # type: ignore[attr-defined]
    assert await engine.advance(workflow.id) is None
    assert await engine.advance("f" * 32) is None


async def test_invalid_model_output_fails_after_corrective_turn(
    container: Container, model: FakeModel
) -> None:
    del model.draft["title"]
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.FAILED
    assert workflow.failure is not None
    assert workflow.failure.code == "model_output_invalid"
    assert "title" in workflow.failure.message
    assert len(model.calls) == 2  # original attempt plus one corrective turn


async def test_cancel_during_step_discards_step_result(container: Container) -> None:
    workflow, _ = await container.service.start(
        encounter_id="cwo-e1", idempotency_key="cancel-race-key", actor="tester"
    )
    # Cancel lands before the runner gets to run; the engine sees a terminal state.
    await container.service.cancel(workflow.id, actor="tester", reason="duplicate request")
    await run_until_idle(container)
    assert (await container.service.get(workflow.id)).state is WorkflowState.CANCELLED


def test_failure_classification() -> None:
    from agentic_core.domain.errors import (
        AuditIntegrityError,
        PolicyDeniedError,
        ProviderUnavailableError,
    )

    assert classify_failure(ProviderUnavailableError("down", attempts=[]))[2] is True
    assert classify_failure(PolicyDeniedError("no", rule_ids=[]))[:1] == ("policy_denied",)
    assert classify_failure(AuditIntegrityError("bad", sequence=1)) == (
        "audit_integrity",
        "bad",
        False,
    )
    assert classify_failure(RuntimeError("secret patient text")) == (
        "internal_error",
        "unexpected RuntimeError",
        False,
    )
