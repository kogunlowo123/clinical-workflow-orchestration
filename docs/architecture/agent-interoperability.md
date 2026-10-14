# Agent interoperability: MCP, A2A and Microsoft Agent Framework

All three surfaces call the same `SkillRouter` (ADR-0007). Every call needs a
bearer agent credential and a `purpose_of_use` of `TREAT` or `HOPERAT`, is
authorized (identity fabric, or local policy when no fabric is configured), and
is recorded in the audit chain with the decision, matched rules and policy
digest.

| Skill | MCP tool | Action (scope) | Returns |
|---|---|---|---|
| `start-care-workflow` | `start_care_workflow` | `workflow:start` | Workflow summary (A2A: Task) |
| `get-care-workflow` | `get_care_workflow` | `workflow:read` | Workflow summary (A2A: Task) |
| `list-pending-approvals` | `list_pending_approvals` | `workflow:list` | Summaries awaiting sign-off |
| `check-medication-safety` | `check_medication_safety` | `medication:check` | Findings and `safe_to_propose` |

Input and output JSON Schemas: `schemas/skill.*.schema.json`.

## MCP

- Official `mcp` Python SDK, `MCPServer` (2.x successor of FastMCP), streamable
  HTTP at `POST /mcp`, stateless with JSON responses: no session affinity is
  needed behind the load balancer.
- Tool input schemas are generated from the typed function signatures; output
  schemas from the Pydantic return models, so clients receive
  `structuredContent`.
- Authorization reads the `Authorization` header of the HTTP request carrying
  the tool call. Domain errors return `isError: true` with a safe message
  (`unauthorized: ...`, `forbidden: ...`); unexpected errors return a generic
  message and are logged.
- Host-header (DNS rebinding) protection is disabled because the service is
  never bound to a browser-reachable localhost; ingress, TLS and bearer
  credentials protect it.

```bash
curl -s "$CWO_URL/mcp" \
  -H "Authorization: Bearer $AGENT_CREDENTIAL" \
  -H 'accept: application/json, text/event-stream' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## A2A

- Agent Card at `GET /.well-known/agent-card.json`: protocol version, name,
  description, `url` (`<public_url>/a2a`), `preferredTransport: JSONRPC`,
  version, provider, capabilities (no streaming, no push, state history),
  input/output modes, the four skills, and a bearer `securitySchemes` entry
  describing the fabric-issued credential.
- `POST /a2a` JSON-RPC 2.0 methods:
  - `message/send`: the first `data` part (or a `text` part containing JSON)
    names the skill and its arguments. Workflow skills return a Task with
    `id = workflow id`; stateless skills return an agent Message with a `data`
    part. `contextId` from the request is echoed.
  - `tasks/get`: `{"id": "<workflow id>", "metadata": {"purpose_of_use": "TREAT"}}`.
  - `message/stream`, `tasks/cancel`, `tasks/resubscribe` and push
    notification methods return `-32004` (unsupported operation). Cancelling
    a clinical workflow is a human decision made over the REST API.
- Task state mapping: pending → `submitted`; retrieving, drafting, validating,
  committing → `working`; awaiting approval → `input-required` (with a status
  message saying a clinician must sign); completed → `completed`; rejected →
  `rejected`; failed → `failed`; cancelled → `canceled`.
- Errors: parse `-32700`, invalid request `-32600`, unknown method `-32601`,
  invalid params `-32602` (with validation details), task not found `-32001`,
  forbidden by policy `-32010`, state conflict `-32011`. Missing or invalid
  credentials are HTTP 401 with an OAuth error body.

## Microsoft Agent Framework

`integrations.agent_framework.ClinicalWorkflowAgent` exposes the orchestrator
as an in-process agent with the framework's agent shape (ADR-0005). It is built
by the container when the identity fabric is configured, using the service's
own fabric credential. Message text must be a JSON object naming the skill and
its arguments, the same payload as an A2A data part.

## Identity fabric

| Call | Purpose |
|---|---|
| `POST /oauth2/token` (jwt-bearer) | The orchestrator exchanges its projected service-account token for its own agent credential (cached, refreshed before expiry, token file re-read each time because the kubelet rotates it) |
| `POST /oauth2/introspect` | Inbound agent credential is active (revocation and suspension epochs checked by the fabric) |
| `POST /v1/authorize` | Decision for an inbound skill call, and for the orchestrator's own EHR write (a fabric `deny` blocks the commit; `require_approval` is satisfied by the clinician sign-off) |
