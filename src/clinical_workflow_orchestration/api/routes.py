"""HTTP routes.

People (care coordinators, signing clinicians, auditors) call ``/v1/*`` with
an enterprise OIDC access token; every call is decided by governance policy.
Agents call ``/a2a`` (JSON-RPC) and ``/mcp`` (MCP streamable HTTP) with an
agent credential.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from agentic_core.domain.classification import Decision
from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from clinical_workflow_orchestration.api.schemas import (
    CancelRequest,
    DecisionRequest,
    MedicationCheckRequest,
    StartWorkflowRequest,
    WorkflowList,
    WorkflowListItem,
    WorkflowView,
)
from clinical_workflow_orchestration.domain.errors import ForbiddenError
from clinical_workflow_orchestration.domain.workflow import WorkflowState
from clinical_workflow_orchestration.interop.a2a import PARSE_ERROR, agent_card
from clinical_workflow_orchestration.interop.skills import FindingSummary, MedicationCheckResult
from clinical_workflow_orchestration.runtime.container import Container
from clinical_workflow_orchestration.security.users import UserPrincipal

router = APIRouter()

WorkflowId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def user(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> UserPrincipal:
    return await container(request).users.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
User = Annotated[UserPrincipal, Depends(user)]


def require(
    c: Container, who: UserPrincipal, action: str, resource_id: str, resource_type: str = "workflow"
) -> None:
    record = c.service.governance.user_action(
        who, action, resource_type=resource_type, resource_id=resource_id, at=c.clock.now()
    )
    c.metrics.policy_decisions.labels(action, record.decision.value).inc()
    if record.decision is not Decision.ALLOW:
        raise ForbiddenError(f"{action} denied: {record.reason}")


# ---------------------------------------------------------------- workflows


@router.post("/v1/workflows", status_code=202, tags=["workflows"])
async def start_workflow(
    body: StartWorkflowRequest,
    c: Deps,
    who: User,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ] = None,
) -> WorkflowView:
    """Start the care-coordination workflow for an encounter (asynchronous)."""
    require(c, who, "workflow:start", body.encounter_id)
    workflow, created = await c.service.start(
        encounter_id=body.encounter_id, idempotency_key=idempotency_key, actor=who.subject
    )
    if not created:
        response.status_code = 200
    response.headers["Location"] = f"/v1/workflows/{workflow.id}"
    return WorkflowView.of(workflow)


@router.get("/v1/workflows", tags=["workflows"])
async def list_workflows(
    c: Deps,
    who: User,
    state: WorkflowState | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> WorkflowList:
    require(c, who, "workflow:list", "*")
    workflows = await c.service.list_workflows(state=state, limit=limit, offset=offset)
    return WorkflowList(
        items=[WorkflowListItem.of(w) for w in workflows], limit=limit, offset=offset
    )


@router.get("/v1/workflows/{workflow_id}", tags=["workflows"])
async def get_workflow(workflow_id: WorkflowId, c: Deps, who: User) -> WorkflowView:
    require(c, who, "workflow:read", workflow_id)
    return WorkflowView.of(await c.service.get(workflow_id))


@router.post("/v1/workflows/{workflow_id}/decision", tags=["sign-off"])
async def decide(
    workflow_id: WorkflowId, body: DecisionRequest, c: Deps, who: User
) -> WorkflowView:
    """Clinician sign-off: approve (write to the EHR), reject, or request changes."""
    workflow = await c.service.decide(
        workflow_id,
        principal=who,
        decision=body.decision,
        draft_digest=body.draft_digest,
        comment=body.comment,
        acknowledgements=body.acknowledgements,
    )
    return WorkflowView.of(workflow)


@router.post("/v1/workflows/{workflow_id}/resume", tags=["workflows"])
async def resume(workflow_id: WorkflowId, c: Deps, who: User) -> WorkflowView:
    require(c, who, "workflow:resume", workflow_id)
    return WorkflowView.of(await c.service.resume(workflow_id, actor=who.subject))


@router.post("/v1/workflows/{workflow_id}/cancel", tags=["workflows"])
async def cancel(workflow_id: WorkflowId, body: CancelRequest, c: Deps, who: User) -> WorkflowView:
    require(c, who, "workflow:cancel", workflow_id)
    return WorkflowView.of(
        await c.service.cancel(workflow_id, actor=who.subject, reason=body.reason)
    )


@router.post("/v1/medication-checks", tags=["medication-safety"])
async def medication_check(
    body: MedicationCheckRequest, c: Deps, who: User
) -> MedicationCheckResult:
    """Check proposed medication changes against the patient's record (read-only)."""
    require(c, who, "medication:check", body.patient_id, "patient")
    findings = await c.service.check_medications(
        patient_id=body.patient_id, proposals=body.proposals, actor=who.subject
    )
    return MedicationCheckResult(
        patient_id=body.patient_id,
        safe_to_propose=not any(f.severity.value == "error" for f in findings),
        findings=[FindingSummary.of(f) for f in findings],
    )


# ------------------------------------------------------------ agent interop


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def a2a_agent_card(c: Deps) -> dict[str, Any]:
    return agent_card(c.settings.public_url_str)


@router.post(
    "/a2a",
    tags=["a2a"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def a2a(
    request: Request, c: Deps, authorization: Annotated[str | None, Header()] = None
) -> JSONResponse:
    """A2A JSON-RPC 2.0 endpoint (``message/send``, ``tasks/get``)."""
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}}
        )
    return JSONResponse(await c.a2a.handle(body, authorization=authorization))


@router.get("/.well-known/jwks.json", tags=["discovery"])
async def jwks(c: Deps) -> dict[str, Any]:
    """Public key verifying sign-off signatures carried in FHIR Provenance."""
    return c.signer.jwks()


# --------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
