"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from agentic_core.domain.errors import AgenticCoreError
from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.routing import Route

from clinical_workflow_orchestration import __version__
from clinical_workflow_orchestration.api.routes import router
from clinical_workflow_orchestration.domain.errors import OrchestrationError
from clinical_workflow_orchestration.interop.mcp_server import (
    build_mcp_server,
    streamable_http_app,
)
from clinical_workflow_orchestration.interop.skills import SkillRouter
from clinical_workflow_orchestration.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("clinical_workflow_orchestration.api")

AGENTIC_STATUS = {"policy_denied": 403, "security_violation": 422}


def create_app(factory: ContainerFactory, *, start_runner: bool = True) -> FastAPI:
    holder: dict[str, Container] = {}

    def skills() -> SkillRouter:
        return holder["container"].skills

    mcp = build_mcp_server(skills)
    mcp_app = streamable_http_app(mcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        holder["container"] = container
        app.state.container = container
        if start_runner:
            container.engine_runner.start()
        log.info("startup", version=__version__)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Clinical Workflow Orchestration",
        version=__version__,
        description=(
            "Multi-agent clinical workflow orchestration on FHIR R4: evidence retrieval, care-plan "
            "drafting, clinical validation, policy-gated clinician sign-off, and EHR write-back "
            "with Provenance. Agent interfaces: A2A at /a2a, MCP at /mcp."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    # MCP streamable HTTP endpoint, served by the SDK's ASGI handler at exactly /mcp.
    for route in mcp_app.routes:
        if isinstance(route, Route):
            app.router.routes.append(route)

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(OrchestrationError)
    async def orchestration_error(_: Request, exc: OrchestrationError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "error_description": exc.message},
            headers=headers,
        )

    @app.exception_handler(AgenticCoreError)
    async def agentic_error(_: Request, exc: AgenticCoreError) -> JSONResponse:
        return JSONResponse(
            status_code=AGENTIC_STATUS.get(exc.code, 502),
            content={"error": exc.code, "error_description": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )

    return app
