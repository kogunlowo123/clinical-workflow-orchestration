"""MCP server (official ``mcp`` Python SDK, streamable HTTP) exposing the clinical skills as tools.

Served stateless with JSON responses so any replica can answer any request
without sticky sessions. Each tool has typed input and output schemas,
authorizes the caller's bearer credential and is audited through the
shared :class:`SkillRouter`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from clinical_workflow_orchestration import __version__
from clinical_workflow_orchestration.domain.careplan import MedicationProposal
from clinical_workflow_orchestration.domain.errors import OrchestrationError
from clinical_workflow_orchestration.interop.skills import (
    SKILL_DESCRIPTIONS,
    CheckMedicationsInput,
    GetWorkflowInput,
    ListPendingInput,
    MedicationCheckResult,
    PendingApprovals,
    PurposeOfUse,
    Skill,
    SkillRouter,
    StartWorkflowInput,
    WorkflowSummary,
)

INSTRUCTIONS = (
    "Clinical workflow orchestration tools. Every tool needs a bearer agent credential and a "
    "purpose_of_use of TREAT or HOPERAT. Starting a workflow never writes to the EHR: a "
    "clinician signs every care plan first."
)


def _authorization(ctx: Context[Any]) -> str | None:
    headers = ctx.headers or {}
    value = headers.get("authorization") or headers.get("Authorization")
    return value if isinstance(value, str) else None


async def _safely[T](call: Awaitable[T]) -> T:
    """Surface domain errors to the calling model as tool errors with a safe message."""
    try:
        return await call
    except OrchestrationError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc


def build_mcp_server(skills: Callable[[], SkillRouter]) -> MCPServer:
    """``skills`` is resolved per call: the router lives in the app's container."""
    server: MCPServer = MCPServer(
        name="clinical-workflow-orchestration",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level="WARNING",
    )

    @server.tool(name="start_care_workflow", description=SKILL_DESCRIPTIONS[Skill.START])
    async def start_care_workflow(
        encounter_id: str,
        purpose_of_use: PurposeOfUse,
        ctx: Context[Any],
        idempotency_key: str | None = None,
    ) -> WorkflowSummary:
        request = StartWorkflowInput(
            encounter_id=encounter_id,
            purpose_of_use=purpose_of_use,
            idempotency_key=idempotency_key,
        )
        return await _safely(
            skills().start(request, authorization=_authorization(ctx), channel="mcp")
        )

    @server.tool(name="get_care_workflow", description=SKILL_DESCRIPTIONS[Skill.GET])
    async def get_care_workflow(
        workflow_id: str, purpose_of_use: PurposeOfUse, ctx: Context[Any]
    ) -> WorkflowSummary:
        request = GetWorkflowInput(workflow_id=workflow_id, purpose_of_use=purpose_of_use)
        return await _safely(
            skills().get(request, authorization=_authorization(ctx), channel="mcp")
        )

    @server.tool(name="list_pending_approvals", description=SKILL_DESCRIPTIONS[Skill.LIST_PENDING])
    async def list_pending_approvals(
        purpose_of_use: PurposeOfUse, ctx: Context[Any], limit: int = 20
    ) -> PendingApprovals:
        request = ListPendingInput(purpose_of_use=purpose_of_use, limit=limit)
        return await _safely(
            skills().list_pending(request, authorization=_authorization(ctx), channel="mcp")
        )

    @server.tool(
        name="check_medication_safety", description=SKILL_DESCRIPTIONS[Skill.CHECK_MEDICATIONS]
    )
    async def check_medication_safety(
        patient_id: str,
        purpose_of_use: PurposeOfUse,
        proposals: list[MedicationProposal],
        ctx: Context[Any],
    ) -> MedicationCheckResult:
        request = CheckMedicationsInput(
            patient_id=patient_id, purpose_of_use=purpose_of_use, proposals=proposals
        )
        return await _safely(
            skills().check_medications(request, authorization=_authorization(ctx), channel="mcp")
        )

    return server


def streamable_http_app(server: MCPServer) -> Starlette:
    # Host-header (DNS rebinding) protection is for servers bound to localhost
    # that browsers can reach; this service sits behind an authenticated
    # ingress and every tool call requires a bearer credential.
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
