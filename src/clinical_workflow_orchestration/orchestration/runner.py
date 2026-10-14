"""In-process workflow runner: bounded concurrency plus a recovery sweep.

``submit`` schedules a workflow for advancement right away (after start, a
clinician decision or a resume). The sweep periodically asks the database
for workflows with engine work and no live lease, which picks up work
orphaned by a crashed replica and automatic retries that have come due.
Leases in the database, not this runner, guarantee single execution across
replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from agentic_core.observability import get_logger

from clinical_workflow_orchestration.domain.ports import Clock, WorkflowRepository
from clinical_workflow_orchestration.orchestration.engine import WorkflowEngine

log = get_logger("clinical_workflow_orchestration.runner")

Sleep = Callable[[float], Awaitable[None]]


class WorkflowRunner:
    def __init__(
        self,
        engine: WorkflowEngine,
        repository: WorkflowRepository,
        clock: Clock,
        *,
        concurrency: int = 8,
        sweep_interval_s: float = 30.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._engine = engine
        self._repo = repository
        self._clock = clock
        self._semaphore = asyncio.Semaphore(concurrency)
        self._interval = sweep_interval_s
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._rerun: set[str] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def active(self) -> int:
        return len(self._tasks)

    def submit(self, workflow_id: str) -> None:
        if self._closed:
            return
        if workflow_id in self._tasks:
            self._rerun.add(workflow_id)
            return
        task = asyncio.get_running_loop().create_task(self._run(workflow_id))
        self._tasks[workflow_id] = task

    async def _run(self, workflow_id: str) -> None:
        try:
            while True:
                self._rerun.discard(workflow_id)
                async with self._semaphore:
                    try:
                        await self._engine.advance(workflow_id)
                    except Exception:
                        log.exception("workflow_advance_failed", workflow=workflow_id)
                if workflow_id not in self._rerun or self._closed:
                    return
        finally:
            self._tasks.pop(workflow_id, None)

    async def sweep_once(self) -> int:
        ids = await self._repo.runnable(now=self._clock.now())
        for workflow_id in ids:
            self.submit(workflow_id)
        return len(ids)

    async def sweep_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("workflow_sweep_failed")
            await self._sleep(self._interval)

    def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.get_running_loop().create_task(self.sweep_forever())

    async def drain(self) -> None:
        """Wait until no workflow is being advanced (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def aclose(self, *, grace_s: float = 10.0) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        if self._tasks:
            # In-flight steps get a grace period; anything left is cancelled and
            # recovered by another replica once its lease expires.
            _, pending = await asyncio.wait(list(self._tasks.values()), timeout=grace_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
