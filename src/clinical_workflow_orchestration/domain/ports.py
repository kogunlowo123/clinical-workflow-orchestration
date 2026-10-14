"""Ports the application layer depends on. Adapters live in infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from clinical_workflow_orchestration.domain.workflow import Workflow, WorkflowState


class WorkflowRepository(Protocol):
    async def add(self, workflow: Workflow) -> Workflow:
        """Insert; when the idempotency key exists, return the stored workflow instead."""
        ...

    async def get(self, workflow_id: str) -> Workflow | None: ...

    async def save(self, workflow: Workflow) -> Workflow:
        """Persist if the stored version equals ``workflow.version``; returns version + 1."""
        ...

    async def list(
        self, *, state: WorkflowState | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[Workflow]: ...

    async def acquire_lease(
        self, workflow_id: str, *, owner: str, now: datetime, until: datetime
    ) -> bool: ...

    async def release_lease(self, workflow_id: str, *, owner: str) -> None: ...

    async def runnable(self, *, now: datetime, limit: int = 50) -> Sequence[str]:
        """Ids of workflows with engine work pending and no live lease (crash recovery)."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...
