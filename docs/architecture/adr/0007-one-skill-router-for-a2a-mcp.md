# ADR-0007: One authorized skill router behind A2A, MCP and the Agent Framework adapter

- Status: Accepted
- Date: 2026-10-14

## Context

Peer agents reach the orchestrator through A2A (JSON-RPC) and MCP (streamable
HTTP), and in-process through the Agent Framework adapter. Three transports
with separate authorization code would drift.

## Decision

- `interop/skills.py` defines four skills with typed inputs and outputs and is
  the only path from any agent transport to the service. It authorizes each
  call (identity fabric or local policy), records the decision in the audit
  chain and counts it.
- Every skill requires `purpose_of_use` (TREAT or HOPERAT) because outputs
  contain PHI; outputs are minimum-necessary summaries without the evidence
  packet.
- MCP is served by the official `mcp` SDK (2.x `MCPServer`, the successor of
  FastMCP) in stateless JSON mode so any replica answers any request. The A2A
  workflow skill returns a Task whose id is the workflow id and whose state is
  `input-required` while waiting for sign-off.
- Agents cannot sign: sign-off is a REST operation that requires a clinician
  OIDC token, and policy denies `careplan:*` to non-human subjects.

## Consequences

- Authorization behaves identically on every transport and is tested per
  transport.
- Adding a skill means one input model, one method and one binding per
  transport.
