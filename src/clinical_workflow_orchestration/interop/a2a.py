"""Agent2Agent (A2A) protocol surface: Agent Card and JSON-RPC 2.0 ``message/send`` / ``tasks/get``.

A care workflow is exposed as an A2A Task whose id is the workflow id. A
caller sends a message with a ``data`` part naming the skill and its
arguments, for example ``{"skill": "start-care-workflow", "encounter_id":
"...", "purpose_of_use": "TREAT"}``. Workflow skills answer with the Task
(``input-required`` while waiting for clinician sign-off); stateless skills
answer with an agent Message carrying a ``data`` part.

Authentication failures are reported at the HTTP layer (401 with an OAuth
error body) as the A2A specification requires; authorization and domain
errors are JSON-RPC errors.
"""

from __future__ import annotations

import json
from typing import Any

from agentic_core.domain.identifiers import new_id
from pydantic import BaseModel, ValidationError

from clinical_workflow_orchestration import __version__
from clinical_workflow_orchestration.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    OrchestrationError,
    UnauthorizedError,
)
from clinical_workflow_orchestration.domain.workflow import WorkflowState
from clinical_workflow_orchestration.interop.skills import (
    SKILL_DESCRIPTIONS,
    GetWorkflowInput,
    Skill,
    SkillRouter,
    WorkflowSummary,
)

PROTOCOL_VERSION = "0.3.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004
FORBIDDEN = -32010  # implementation-defined server error range (-32000..-32099)
CONFLICT = -32011

UNSUPPORTED_METHODS = frozenset({"message/stream", "tasks/resubscribe", "tasks/cancel"})
ERROR_CODES = {
    NotFoundError.code: TASK_NOT_FOUND,
    ForbiddenError.code: FORBIDDEN,
    InvalidStateError.code: CONFLICT,
    ConflictError.code: CONFLICT,
}

TASK_STATES: dict[WorkflowState, str] = {
    WorkflowState.PENDING: "submitted",
    WorkflowState.RETRIEVING_EVIDENCE: "working",
    WorkflowState.DRAFTING: "working",
    WorkflowState.VALIDATING: "working",
    WorkflowState.AWAITING_APPROVAL: "input-required",
    WorkflowState.COMMITTING: "working",
    WorkflowState.COMPLETED: "completed",
    WorkflowState.REJECTED: "rejected",
    WorkflowState.FAILED: "failed",
    WorkflowState.CANCELLED: "canceled",
}

STATUS_TEXT: dict[str, str] = {
    "input-required": "Awaiting clinician sign-off of the drafted care plan.",
    "failed": "Workflow step failed; a care coordinator can resume or cancel it.",
}


def agent_card(public_url: str) -> dict[str, Any]:
    base = public_url.rstrip("/")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "clinical-workflow-orchestrator",
        "description": (
            "Multi-agent clinical workflow orchestration on FHIR R4. Drafts post-encounter care "
            "plans, validates them for medication and follow-up safety, and writes them to the "
            "EHR only after clinician sign-off. Also served as MCP tools at "
            f"{base}/mcp."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": f"{base}/a2a", "transport": "JSONRPC"}],
        "version": __version__,
        "provider": {
            "organization": "kogunlowo123",
            "url": "https://github.com/kogunlowo123/clinical-workflow-orchestration",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": skill.value,
                "name": skill.value.replace("-", " "),
                "description": SKILL_DESCRIPTIONS[skill],
                "tags": ["fhir", "care-coordination", "clinical-safety"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
            for skill in Skill
        ],
        "securitySchemes": {
            "agentCredential": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Short-lived agent credential issued by the agent identity fabric "
                    "(RFC 7523 jwt-bearer); PHI skills also require purpose_of_use."
                ),
            }
        },
        "security": [{"agentCredential": []}],
    }


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def task_of(summary: WorkflowSummary, *, context_id: str | None = None) -> dict[str, Any]:
    state = TASK_STATES[summary.state]
    status: dict[str, Any] = {"state": state, "timestamp": summary.updated_at.isoformat()}
    if state in STATUS_TEXT:
        status["message"] = {
            "kind": "message",
            "role": "agent",
            "messageId": new_id(),
            "parts": [{"kind": "text", "text": STATUS_TEXT[state]}],
        }
    return {
        "kind": "task",
        "id": summary.workflow_id,
        "contextId": context_id or summary.workflow_id,
        "status": status,
        "artifacts": [
            {
                "artifactId": f"{summary.workflow_id}-summary",
                "name": "care-workflow-summary",
                "parts": [{"kind": "data", "data": summary.model_dump(mode="json")}],
            }
        ],
        "metadata": {"workflowState": summary.state.value},
    }


def _skill_call(message: Any) -> tuple[Skill, dict[str, Any]]:
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        raise ValueError("params.message with parts is required")
    for part in message["parts"]:
        if not isinstance(part, dict):
            continue
        payload: Any = None
        if part.get("kind") == "data":
            payload = part.get("data")
        elif part.get("kind") == "text" and isinstance(part.get("text"), str):
            try:
                payload = json.loads(part["text"])
            except ValueError:
                continue
        if isinstance(payload, dict) and "skill" in payload:
            arguments = {k: v for k, v in payload.items() if k != "skill"}
            try:
                return Skill(str(payload["skill"])), arguments
            except ValueError:
                raise ValueError(f"unknown skill {payload['skill']!r}") from None
    raise ValueError("message has no data part naming a skill")


class A2AHandler:
    def __init__(self, skills: SkillRouter) -> None:
        self._skills = skills

    async def handle(self, body: Any, *, authorization: str | None) -> dict[str, Any]:
        if not isinstance(body, dict):
            return _error(None, INVALID_REQUEST, "request must be a JSON-RPC 2.0 object")
        request_id = body.get("id")
        if body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            return _error(request_id, INVALID_REQUEST, "jsonrpc must be 2.0 and method a string")
        raw_params = body.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        method: str = body["method"]
        handlers = {"message/send": self._send, "tasks/get": self._get}
        handler = handlers.get(method)
        if handler is None:
            unsupported = method in UNSUPPORTED_METHODS or method.startswith(
                "tasks/pushNotificationConfig/"
            )
            code = UNSUPPORTED_OPERATION if unsupported else METHOD_NOT_FOUND
            return _error(request_id, code, f"method {method} is not supported")
        try:
            return _result(request_id, await handler(params, authorization))
        except UnauthorizedError:
            raise
        except (ValueError, ValidationError) as exc:
            return _error(request_id, INVALID_PARAMS, "invalid params", _details(exc))
        except OrchestrationError as exc:
            return _error(request_id, ERROR_CODES.get(exc.code, INTERNAL_ERROR), exc.message)

    async def _send(self, params: dict[str, Any], authorization: str | None) -> dict[str, Any]:
        message = params.get("message")
        skill, arguments = _skill_call(message)
        output: BaseModel = await self._skills.invoke(
            skill, arguments, authorization=authorization, channel="a2a"
        )
        context_id = message.get("contextId") if isinstance(message, dict) else None
        if isinstance(output, WorkflowSummary):
            return task_of(output, context_id=context_id if isinstance(context_id, str) else None)
        return {
            "kind": "message",
            "role": "agent",
            "messageId": new_id(),
            **({"contextId": context_id} if isinstance(context_id, str) else {}),
            "parts": [{"kind": "data", "data": output.model_dump(mode="json")}],
        }

    async def _get(self, params: dict[str, Any], authorization: str | None) -> dict[str, Any]:
        raw = params.get("metadata")
        metadata: dict[str, Any] = raw if isinstance(raw, dict) else {}
        request = GetWorkflowInput.model_validate(
            {"workflow_id": params.get("id"), "purpose_of_use": metadata.get("purpose_of_use")}
        )
        summary = await self._skills.get(request, authorization=authorization, channel="a2a")
        return task_of(summary)


def _details(exc: Exception) -> list[dict[str, Any]] | str:
    if isinstance(exc, ValidationError):
        return [
            {"loc": [str(p) for p in error["loc"]], "msg": error["msg"]} for error in exc.errors()
        ]
    return str(exc)
