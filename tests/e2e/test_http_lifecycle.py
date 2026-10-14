"""Full lifecycles over HTTP, exactly as care teams and other agents use the service."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import jwt
import pytest
from agentic_core.audit import AuditEvent, verify_chain
from fastapi.testclient import TestClient

from tests.conftest import (
    RISKY_DRAFT,
    FakeFhirServer,
    FakeIdp,
    FakeModel,
    agent_headers,
    clinician_headers,
    coordinator_headers,
    wait_for,
)

pytestmark = pytest.mark.e2e

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def a2a(client: TestClient, headers: dict[str, str], method: str, params: Any) -> dict[str, Any]:
    response = client.post(
        "/a2a",
        headers=headers,
        json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def send(client: TestClient, headers: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    message = {
        "kind": "message",
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "contextId": "discharge-ctx-1",
        "parts": [{"kind": "data", "data": data}],
    }
    return a2a(client, headers, "message/send", {"message": message})


def mcp(client: TestClient, headers: dict[str, str], method: str, params: Any) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={**MCP_HEADERS, **headers},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_rest_lifecycle(
    client: TestClient, user_idp: FakeIdp, fhir: FakeFhirServer, model: FakeModel
) -> None:
    coordinator = coordinator_headers(user_idp)
    started = client.post(
        "/v1/workflows",
        json={"encounter_id": "cwo-e1"},
        headers={**coordinator, "Idempotency-Key": "discharge-cwo-e1-2026-10-12"},
    )
    assert started.status_code == 202, started.text
    assert started.headers["x-content-type-options"] == "nosniff"
    workflow_id = started.json()["workflow_id"]
    assert started.headers["location"] == f"/v1/workflows/{workflow_id}"
    replay = client.post(
        "/v1/workflows",
        json={"encounter_id": "cwo-e1"},
        headers={**coordinator, "Idempotency-Key": "discharge-cwo-e1-2026-10-12"},
    )
    assert replay.status_code == 200
    assert replay.json()["workflow_id"] == workflow_id

    clinician = clinician_headers(user_idp)
    waiting = wait_for(client, workflow_id, clinician, {"awaiting_approval"})
    assert waiting["validation"]["outcome"] == "needs_attention"
    assert waiting["approval"]["decision"] == "require_approval"
    assert waiting["evidence"]["patient_id"] == "cwo-p1"
    assert fhir.transactions == []  # nothing written before sign-off

    listed = client.get("/v1/workflows", params={"state": "awaiting_approval"}, headers=coordinator)
    assert [w["workflow_id"] for w in listed.json()["items"]] == [workflow_id]

    # A care coordinator cannot sign; a clinician can.
    decision = {"decision": "approve", "draft_digest": waiting["draft"]["digest"], "comment": "ok"}
    forbidden = client.post(
        f"/v1/workflows/{workflow_id}/decision", json=decision, headers=coordinator
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == "forbidden"
    approved = client.post(
        f"/v1/workflows/{workflow_id}/decision", json=decision, headers=clinician
    )
    assert approved.status_code == 200, approved.text
    done = wait_for(client, workflow_id, clinician, {"completed"})
    plan_id = done["commit"]["care_plan"].split("/")[1]
    assert fhir.store["CarePlan"][plan_id]["status"] == "active"

    # The Provenance signature verifies against the published JWKS.
    provenance = fhir.store["Provenance"][done["commit"]["provenance"].split("/")[1]]
    token = base64.b64decode(provenance["signature"][0]["data"]).decode()
    jwk = client.get("/.well-known/jwks.json").json()["keys"][0]
    claims = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["EdDSA"])
    assert claims["wf"] == workflow_id

    metrics = client.get("/metrics").text
    assert 'cwo_signoffs_total{decision="approve"} 1.0' in metrics
    assert "cwo_model_tokens_total" in metrics
    assert 'cwo_workflow_transitions_total{from_state="committing",to_state="completed"}' in metrics

    # Every decision and transition is in the signed, hash-chained audit log.
    container = client.app.state.container  # type: ignore[attr-defined]
    path = container.settings.audit_log_path
    events = [AuditEvent.model_validate_json(line) for line in path.read_text().splitlines()]
    assert verify_chain(events, {container.signer.key_id: container.signer.public_key}) == len(
        events
    )
    actions = [e.action for e in events]
    assert "workflow.signoff.approve" in actions
    assert "workflow.completed" in actions
    assert not any("Rowan" in line for line in path.read_text().splitlines())


def test_rejection_and_cancel_over_http(
    client: TestClient, user_idp: FakeIdp, model: FakeModel
) -> None:
    model.draft = dict(RISKY_DRAFT)
    coordinator = coordinator_headers(user_idp)
    clinician = clinician_headers(user_idp)
    first = client.post(
        "/v1/workflows", json={"encounter_id": "cwo-e1"}, headers=coordinator
    ).json()
    waiting = wait_for(client, first["workflow_id"], clinician, {"awaiting_approval"})
    assert waiting["validation"]["outcome"] == "blocked"
    unacknowledged = client.post(
        f"/v1/workflows/{first['workflow_id']}/decision",
        json={"decision": "approve", "draft_digest": waiting["draft"]["digest"]},
        headers=clinician,
    )
    assert unacknowledged.status_code == 403
    rejected = client.post(
        f"/v1/workflows/{first['workflow_id']}/decision",
        json={
            "decision": "reject",
            "draft_digest": waiting["draft"]["digest"],
            "comment": "unsafe",
        },
        headers=clinician,
    )
    assert rejected.json()["state"] == "rejected"

    second = client.post(
        "/v1/workflows", json={"encounter_id": "cwo-e1"}, headers=coordinator
    ).json()
    wait_for(client, second["workflow_id"], clinician, {"awaiting_approval"})
    cancelled = client.post(
        f"/v1/workflows/{second['workflow_id']}/cancel",
        json={"reason": "patient readmitted"},
        headers=coordinator,
    )
    assert cancelled.json()["state"] == "cancelled"
    resume = client.post(f"/v1/workflows/{second['workflow_id']}/resume", headers=coordinator)
    assert resume.status_code == 409


def test_failed_workflow_can_be_resumed_over_http(
    client: TestClient, user_idp: FakeIdp, fhir: FakeFhirServer
) -> None:
    coordinator = coordinator_headers(user_idp)
    fhir.fail_with = [400]
    started = client.post(
        "/v1/workflows", json={"encounter_id": "cwo-e1"}, headers=coordinator
    ).json()
    failed = wait_for(client, started["workflow_id"], coordinator, {"failed"})
    assert failed["failure"]["code"] == "fhir_error"
    resumed = client.post(f"/v1/workflows/{started['workflow_id']}/resume", headers=coordinator)
    assert resumed.status_code == 200
    wait_for(client, started["workflow_id"], coordinator, {"awaiting_approval"})


def test_medication_check_endpoint(client: TestClient, user_idp: FakeIdp) -> None:
    response = client.post(
        "/v1/medication-checks",
        json={
            "patient_id": "cwo-p1",
            "proposals": [{"action": "start", "display": "amoxicillin 500 MG Oral Capsule"}],
        },
        headers=coordinator_headers(user_idp),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["safe_to_propose"] is False
    assert body["findings"][0]["check"] == "allergy"


def test_authentication_and_validation_errors(client: TestClient, user_idp: FakeIdp) -> None:
    missing = client.get("/v1/workflows")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json() == {"error": "unauthorized", "error_description": "bearer token required"}
    auditor = user_idp.mint(
        sub="aud@hospital.test",
        aud="api://clinical-workflow-orchestration",
        roles=["clinical-auditor"],
    )
    headers = {"Authorization": f"Bearer {auditor}"}
    assert client.get("/v1/workflows", headers=headers).status_code == 200
    assert (
        client.post("/v1/workflows", json={"encounter_id": "cwo-e1"}, headers=headers).status_code
        == 403
    )
    bad = client.post(
        "/v1/workflows", json={"encounter_id": "../x"}, headers=coordinator_headers(user_idp)
    )
    assert bad.status_code == 422
    assert bad.json()["error"] == "invalid_request"
    assert client.get(f"/v1/workflows/{'a' * 32}", headers=headers).status_code == 404


def test_a2a_lifecycle(client: TestClient, agent_idp: FakeIdp, user_idp: FakeIdp) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://cwo.hospital.test/a2a"
    assert {s["id"] for s in card["skills"]} == {
        "start-care-workflow",
        "get-care-workflow",
        "list-pending-approvals",
        "check-medication-safety",
    }
    assert card["securitySchemes"]["agentCredential"]["scheme"] == "bearer"

    headers = agent_headers(agent_idp)
    started = send(
        client,
        headers,
        {"skill": "start-care-workflow", "encounter_id": "cwo-e1", "purpose_of_use": "TREAT"},
    )["result"]
    assert started["kind"] == "task"
    assert started["contextId"] == "discharge-ctx-1"
    task_id = started["id"]
    wait_for(client, task_id, coordinator_headers(user_idp), {"awaiting_approval"})
    task = a2a(
        client, headers, "tasks/get", {"id": task_id, "metadata": {"purpose_of_use": "TREAT"}}
    )["result"]
    assert task["status"]["state"] == "input-required"
    assert "clinician sign-off" in task["status"]["message"]["parts"][0]["text"]
    summary = task["artifacts"][0]["parts"][0]["data"]
    assert summary["validation_outcome"] == "needs_attention"

    pending = send(client, headers, {"skill": "list-pending-approvals", "purpose_of_use": "TREAT"})[
        "result"
    ]
    assert pending["kind"] == "message"
    assert [i["workflow_id"] for i in pending["parts"][0]["data"]["items"]] == [task_id]

    text_part = {
        "message": {
            "role": "user",
            "messageId": "m2",
            "parts": [
                {
                    "kind": "text",
                    "text": json.dumps(
                        {
                            "skill": "check-medication-safety",
                            "patient_id": "cwo-p1",
                            "purpose_of_use": "TREAT",
                            "proposals": [{"action": "start", "display": "ibuprofen 400 MG"}],
                        }
                    ),
                }
            ],
        }
    }
    checked = a2a(client, headers, "message/send", text_part)["result"]
    assert checked["parts"][0]["data"]["safe_to_propose"] is True


def test_a2a_errors(client: TestClient, agent_idp: FakeIdp) -> None:
    headers = agent_headers(agent_idp)
    unauthenticated = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"skill": "list-pending-approvals", "purpose_of_use": "TREAT"},
                        }
                    ]
                }
            },
        },
    )
    assert unauthenticated.status_code == 401
    parse = client.post(
        "/a2a", content=b"{not json", headers={**headers, "content-type": "application/json"}
    )
    assert parse.json()["error"]["code"] == -32700
    assert client.post("/a2a", json=[1], headers=headers).json()["error"]["code"] == -32600
    assert (
        client.post("/a2a", json={"jsonrpc": "1.0", "method": "x"}, headers=headers).json()[
            "error"
        ]["code"]
        == -32600
    )
    assert a2a(client, headers, "tasks/cancel", {})["error"]["code"] == -32004
    assert a2a(client, headers, "tasks/pushNotificationConfig/set", {})["error"]["code"] == -32004
    assert a2a(client, headers, "agent/dance", {})["error"]["code"] == -32601
    assert send(client, headers, {"skill": "unknown-skill"})["error"]["code"] == -32602
    assert (
        a2a(
            client,
            headers,
            "message/send",
            {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
        )["error"]["code"]
        == -32602
    )
    assert a2a(client, headers, "message/send", {})["error"]["code"] == -32602
    missing_purpose = send(
        client, headers, {"skill": "start-care-workflow", "encounter_id": "cwo-e1"}
    )
    assert missing_purpose["error"]["code"] == -32602
    not_found = a2a(
        client, headers, "tasks/get", {"id": "0" * 32, "metadata": {"purpose_of_use": "TREAT"}}
    )
    assert not_found["error"]["code"] == -32001
    out_of_scope = send(
        client,
        agent_headers(agent_idp, scope="workflow:read"),
        {"skill": "start-care-workflow", "encounter_id": "cwo-e1", "purpose_of_use": "TREAT"},
    )
    assert out_of_scope["error"]["code"] == -32010


def test_mcp_tools(client: TestClient, agent_idp: FakeIdp, user_idp: FakeIdp) -> None:
    headers = agent_headers(agent_idp)
    tools = mcp(client, headers, "tools/list", {})["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {
        "start_care_workflow",
        "get_care_workflow",
        "list_pending_approvals",
        "check_medication_safety",
    }
    start_schema = by_name["start_care_workflow"]["inputSchema"]
    assert set(start_schema["required"]) == {"encounter_id", "purpose_of_use"}
    assert "workflow_id" in by_name["start_care_workflow"]["outputSchema"]["properties"]

    started = mcp(
        client,
        headers,
        "tools/call",
        {
            "name": "start_care_workflow",
            "arguments": {"encounter_id": "cwo-e1", "purpose_of_use": "TREAT"},
        },
    )["result"]
    assert started["isError"] is False
    workflow_id = started["structuredContent"]["workflow_id"]
    wait_for(client, workflow_id, coordinator_headers(user_idp), {"awaiting_approval"})
    fetched = mcp(
        client,
        headers,
        "tools/call",
        {
            "name": "get_care_workflow",
            "arguments": {"workflow_id": workflow_id, "purpose_of_use": "TREAT"},
        },
    )["result"]["structuredContent"]
    assert fetched["state"] == "awaiting_approval"
    assert fetched["activities"]
    pending = mcp(
        client,
        headers,
        "tools/call",
        {"name": "list_pending_approvals", "arguments": {"purpose_of_use": "HOPERAT"}},
    )["result"]["structuredContent"]
    assert len(pending["items"]) == 1
    check = mcp(
        client,
        headers,
        "tools/call",
        {
            "name": "check_medication_safety",
            "arguments": {
                "patient_id": "cwo-p1",
                "purpose_of_use": "TREAT",
                "proposals": [{"action": "start", "display": "losartan 50 MG Oral Tablet"}],
            },
        },
    )["result"]["structuredContent"]
    assert check["safe_to_propose"] is False

    denied = mcp(
        client,
        {},
        "tools/call",
        {"name": "list_pending_approvals", "arguments": {"purpose_of_use": "TREAT"}},
    )["result"]
    assert denied["isError"] is True
    assert "bearer token required" in denied["content"][0]["text"]
