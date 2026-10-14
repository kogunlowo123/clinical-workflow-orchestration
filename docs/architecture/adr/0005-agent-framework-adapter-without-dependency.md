# ADR-0005: Microsoft Agent Framework integration through a structural adapter

- Status: Accepted
- Date: 2026-10-14

## Context

Teams on Azure compose agents with Microsoft Agent Framework. The framework
evolves quickly; pinning it into this service would couple our release cadence
and dependency surface to it, while most callers reach us over A2A or MCP.

## Decision

`integrations/agent_framework.py` implements the framework's agent protocol
shape structurally: `name`, `description`, `get_new_thread()`,
`run(messages, thread=...)` returning a response with `messages`, `text` and
`value`, and `run_stream(...)`. A host embedding this package passes the
adapter wherever an agent is expected. Calls go through the same
`SkillRouter` as A2A and MCP, authorized with the host's own agent credential
and audited on channel `agent-framework`.

## Consequences

- No framework dependency; the adapter is tested directly.
- If the framework's protocol changes, the adapter changes in one file.
- Out-of-process hosts use A2A, which the framework also speaks.
