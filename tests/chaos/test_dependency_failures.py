"""Fault injection: the orchestrator fails safe, recovers, and never writes twice."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from clinical_workflow_orchestration.domain.workflow import WorkflowState
from clinical_workflow_orchestration.infrastructure.database import make_engine
from clinical_workflow_orchestration.runtime.container import Container
from tests.conftest import FakeClock, FakeFhirServer, FakeModel
from tests.helpers import approve, run_until_idle, start_and_wait

pytestmark = pytest.mark.chaos


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200  # liveness unaffected
    finally:
        container.engine = healthy


async def test_fhir_brownout_is_absorbed_by_retries(
    container: Container, fhir: FakeFhirServer
) -> None:
    fhir.fail_with = [503, 502]  # two transient failures inside one read
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.AWAITING_APPROVAL


async def test_fhir_outage_fails_retryable_then_recovers(
    container: Container, fhir: FakeFhirServer, clock: FakeClock
) -> None:
    fhir.fail_with = [503] * 3
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.FAILED
    assert workflow.failure is not None
    assert workflow.failure.retryable
    clock.advance(31)
    await container.engine_runner.sweep_once()
    await run_until_idle(container)
    assert (await container.service.get(workflow.id)).state is WorkflowState.AWAITING_APPROVAL


async def test_primary_model_outage_fails_over(container: Container, model: FakeModel) -> None:
    model.fail_primary = 100
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.AWAITING_APPROVAL
    assert workflow.draft is not None
    assert workflow.draft.model.provider == "in-cluster-vllm"


async def test_review_outage_degrades_to_deterministic_checks(
    container: Container, model: FakeModel
) -> None:
    model.fail_after_calls = 1  # drafting succeeds, every review attempt fails
    workflow = await start_and_wait(container)
    assert workflow.state is WorkflowState.AWAITING_APPROVAL
    assert workflow.validation is not None
    assert workflow.validation.review_status.value == "unavailable"


async def test_lost_commit_response_is_recovered_without_duplicates(
    container: Container, fhir: FakeFhirServer, clock: FakeClock
) -> None:
    """The EHR commits the transaction but the response is lost (gateway timeout)."""
    waiting = await start_and_wait(container)
    original = fhir._transaction
    state = {"lose_response": True}

    def commit_then_lose_response(bundle):  # type: ignore[no-untyped-def]
        response = original(bundle)
        if state["lose_response"]:
            state["lose_response"] = False
            return httpx.Response(504)
        return response

    fhir._transaction = commit_then_lose_response  # type: ignore[method-assign]
    failed = await approve(container, waiting)
    assert failed.state is WorkflowState.FAILED
    assert failed.failure is not None
    assert failed.failure.step is WorkflowState.COMMITTING
    assert failed.failure.retryable
    clock.advance(31)
    await container.engine_runner.sweep_once()
    await run_until_idle(container)
    done = await container.service.get(waiting.id)
    assert done.state is WorkflowState.COMPLETED
    assert done.commit is not None
    assert done.commit.bundle_digest == "recovered"
    assert len(fhir.transactions) == 1
    assert len(fhir.all("CarePlan")) == 1
    assert len(fhir.all("Provenance")) == 1


async def test_orphaned_lease_is_taken_over(container: Container, clock: FakeClock) -> None:
    from tests.unit.test_domain_and_persistence import make_workflow

    repo = container.service._repo  # type: ignore[attr-defined]
    orphan = make_workflow("orphan-key").model_copy(
        update={"created_at": clock.now(), "updated_at": clock.now()}
    )
    await repo.add(orphan)
    now = clock.now()
    await repo.acquire_lease(orphan.id, owner="dead", now=now, until=now + timedelta(seconds=60))
    assert await container.engine_runner.sweep_once() == 0
    clock.advance(61)
    assert await container.engine_runner.sweep_once() == 1
    await run_until_idle(container)
    assert (await container.service.get(orphan.id)).state is WorkflowState.AWAITING_APPROVAL


async def test_audit_failure_stops_the_step(container: Container) -> None:
    """If evidence of a transition cannot be written, the transition is not acknowledged."""

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    container.audit._sink.append = broken_append  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        await container.service.start(
            encounter_id="cwo-e1", idempotency_key="audit-failure-key", actor="tester"
        )
