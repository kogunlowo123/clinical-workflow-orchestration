"""SQLAlchemy Core persistence. PostgreSQL in production; any async dialect works.

The workflow aggregate is stored as a JSON document with the columns needed
for querying, optimistic concurrency (``version``), crash-recovery leases
(``lease_owner``, ``lease_expires_at``) and automatic retry scheduling
(``next_attempt_at``) promoted to real columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from clinical_workflow_orchestration.domain.errors import ConflictError, NotFoundError
from clinical_workflow_orchestration.domain.workflow import RUNNABLE, Workflow, WorkflowState

metadata = MetaData()

workflows = Table(
    "workflows",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("encounter_id", String(64), nullable=False),
    Column("state", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Index("ix_workflows_state", "state"),
    Index("ix_workflows_encounter", "encounter_id"),
    Index("ix_workflows_next_attempt", "next_attempt_at"),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _next_attempt(workflow: Workflow) -> datetime | None:
    if workflow.state is WorkflowState.FAILED and workflow.failure is not None:
        return workflow.failure.next_retry_at
    return None


class SqlWorkflowRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, workflow: Workflow) -> Workflow:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(workflows).values(
                        id=workflow.id,
                        idempotency_key=workflow.idempotency_key,
                        encounter_id=workflow.encounter_id,
                        state=workflow.state.value,
                        version=workflow.version,
                        document=workflow.model_dump_json(),
                        created_at=workflow.created_at,
                        updated_at=workflow.updated_at,
                    )
                )
        except IntegrityError:
            async with self._engine.connect() as conn:
                row = (
                    await conn.execute(
                        select(workflows.c.document).where(
                            workflows.c.idempotency_key == workflow.idempotency_key
                        )
                    )
                ).first()
            if row is None:
                raise ConflictError(f"workflow {workflow.id} already exists") from None
            return Workflow.model_validate_json(row[0])
        return workflow

    async def get(self, workflow_id: str) -> Workflow | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(workflows.c.document).where(workflows.c.id == workflow_id)
                )
            ).first()
        return Workflow.model_validate_json(row[0]) if row else None

    async def save(self, workflow: Workflow) -> Workflow:
        stored = workflow.model_copy(update={"version": workflow.version + 1})
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workflows)
                .where(and_(workflows.c.id == workflow.id, workflows.c.version == workflow.version))
                .values(
                    state=stored.state.value,
                    version=stored.version,
                    document=stored.model_dump_json(),
                    updated_at=stored.updated_at,
                    next_attempt_at=_next_attempt(stored),
                )
            )
            if result.rowcount == 0:
                exists = (
                    await conn.execute(select(workflows.c.id).where(workflows.c.id == workflow.id))
                ).first()
                if exists is None:
                    raise NotFoundError(f"workflow {workflow.id} not found")
                raise ConflictError(
                    f"workflow {workflow.id} was modified concurrently (version {workflow.version})"
                )
        return stored

    async def list(
        self, *, state: WorkflowState | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[Workflow]:
        query = (
            select(workflows.c.document)
            .order_by(workflows.c.created_at.desc(), workflows.c.id)
            .limit(limit)
            .offset(offset)
        )
        if state is not None:
            query = query.where(workflows.c.state == state.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Workflow.model_validate_json(r[0]) for r in rows]

    async def acquire_lease(
        self, workflow_id: str, *, owner: str, now: datetime, until: datetime
    ) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workflows)
                .where(
                    and_(
                        workflows.c.id == workflow_id,
                        or_(
                            workflows.c.lease_owner.is_(None),
                            workflows.c.lease_owner == owner,
                            workflows.c.lease_expires_at < _aware(now),
                        ),
                    )
                )
                .values(lease_owner=owner, lease_expires_at=_aware(until))
            )
        return bool(result.rowcount)

    async def release_lease(self, workflow_id: str, *, owner: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(workflows)
                .where(and_(workflows.c.id == workflow_id, workflows.c.lease_owner == owner))
                .values(lease_owner=None, lease_expires_at=None)
            )

    async def runnable(self, *, now: datetime, limit: int = 50) -> Sequence[str]:
        moment = _aware(now)
        unleased = or_(workflows.c.lease_owner.is_(None), workflows.c.lease_expires_at < moment)
        query = (
            select(workflows.c.id)
            .where(
                and_(
                    unleased,
                    or_(
                        workflows.c.state.in_([s.value for s in RUNNABLE]),
                        and_(
                            workflows.c.state == WorkflowState.FAILED.value,
                            workflows.c.next_attempt_at <= moment,
                        ),
                    ),
                )
            )
            .order_by(workflows.c.updated_at)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [str(r[0]) for r in rows]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
