"""Repository and full workflow behaviour against a real PostgreSQL (CI service container).

Runs when CWO_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://cwo:cwo@localhost:5432/cwo
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from clinical_workflow_orchestration.domain.errors import ConflictError
from clinical_workflow_orchestration.domain.workflow import Workflow, WorkflowState
from clinical_workflow_orchestration.infrastructure.database import (
    SqlWorkflowRepository,
    create_schema,
    make_engine,
)
from clinical_workflow_orchestration.runtime.container import Container
from tests.helpers import approve, start_and_wait

URL = os.environ.get("CWO_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_repository_round_trip() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    repo = SqlWorkflowRepository(engine)
    now = datetime.now(UTC)
    workflow = Workflow(
        id=uuid.uuid4().hex,
        idempotency_key=f"pg-{uuid.uuid4()}",
        encounter_id="cwo-e1",
        state=WorkflowState.PENDING,
        created_by="integration",
        created_at=now,
        updated_at=now,
    )
    assert (await repo.add(workflow)).id == workflow.id
    duplicate = workflow.model_copy(update={"id": uuid.uuid4().hex})
    assert (await repo.add(duplicate)).id == workflow.id  # idempotency key wins
    saved = await repo.save(
        workflow.transition(WorkflowState.RETRIEVING_EVIDENCE, at=now, actor="t", reason="r")
    )
    with pytest.raises(ConflictError):
        await repo.save(workflow)
    assert (await repo.get(workflow.id)) == saved
    assert await repo.acquire_lease(
        workflow.id, owner="a", now=now, until=now + timedelta(minutes=1)
    )
    assert not await repo.acquire_lease(
        workflow.id, owner="b", now=now, until=now + timedelta(minutes=1)
    )
    assert workflow.id not in await repo.runnable(now=now)
    await repo.release_lease(workflow.id, owner="a")
    assert workflow.id in await repo.runnable(now=now)
    await engine.dispose()


async def test_full_workflow_on_postgres(container: Container) -> None:
    """The conftest settings fixture uses CWO_TEST_DATABASE_URL when set."""
    assert container.settings.database_url.startswith("postgresql")
    done = await approve(container, await start_and_wait(container))
    assert done.state is WorkflowState.COMPLETED
