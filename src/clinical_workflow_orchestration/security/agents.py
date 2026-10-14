"""Authorization of inbound calls from other agents (A2A and MCP).

Two implementations, chosen at start-up:

* :class:`FabricAgentAuthorizer` when the identity fabric URL is configured:
  the caller's credential is introspected (revocation and epoch checks
  happen in the fabric) and the action is decided by the fabric's policy.
* :class:`LocalAgentAuthorizer` otherwise: the credential is verified
  against the configured agent-token JWKS and decided by this service's own
  policy set. With no verifier configured every call is rejected; there is
  no allow-all mode.

In both modes the resource classification ceiling carried by the
credential is enforced, and the decision is returned for auditing.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Protocol

from agentic_core.domain.classification import Decision, RiskLevel
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict

from clinical_workflow_orchestration.domain.errors import UnauthorizedError
from clinical_workflow_orchestration.integrations.identity_fabric import IdentityFabricClient
from clinical_workflow_orchestration.security.jwks import KeyResolver, verify_jwt


class CallerDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    caller: str
    action: str
    reason: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    mode: str


class AgentAuthorizer(Protocol):
    async def authorize(
        self,
        authorization: str | None,
        *,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
    ) -> CallerDecision: ...


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("agent bearer token required")
    token = authorization[7:].strip()
    if not token:
        raise UnauthorizedError("agent bearer token required")
    return token


class FabricAgentAuthorizer:
    def __init__(self, fabric: IdentityFabricClient) -> None:
        self._fabric = fabric

    async def authorize(
        self,
        authorization: str | None,
        *,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
    ) -> CallerDecision:
        token = bearer_token(authorization)
        introspection = await self._fabric.introspect(token)
        if not introspection.active or not introspection.sub:
            raise UnauthorizedError("agent credential is not active")
        decision = await self._fabric.authorize(
            token,
            action=action,
            resource_type=resource.type,
            resource_id=resource.id,
            classification=resource.classification,
            attributes=dict(resource.attributes),
            environment=environment,
        )
        return CallerDecision(
            decision=decision.decision,
            caller=introspection.sub,
            action=action,
            reason=decision.reason,
            matched_rules=decision.matched_rules,
            policy_version=decision.policy_version,
            policy_digest=decision.policy_digest,
            mode="identity-fabric",
        )


class LocalAgentAuthorizer:
    def __init__(
        self,
        resolver: KeyResolver | None,
        *,
        issuer: str | None,
        audience: str | None,
        policy: PolicyEngine,
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._policy = policy

    async def authorize(
        self,
        authorization: str | None,
        *,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
    ) -> CallerDecision:
        token = bearer_token(authorization)
        if self._resolver is None or self._issuer is None or self._audience is None:
            raise UnauthorizedError("no agent credential verifier is configured")
        claims = await verify_jwt(
            token, self._resolver, issuer=self._issuer, audience=self._audience
        )
        scopes = sorted(str(claims.get("scope", "")).split())
        max_cls = claims.get("max_cls", 0)
        ceiling = max_cls if isinstance(max_cls, int) else 0
        risk = claims.get("risk", 1)
        subject = Subject(
            id=str(claims["sub"]),
            kind="agent",
            attributes={
                "scopes": scopes,
                "action_in_scope": any(fnmatch.fnmatchcase(action, s) for s in scopes),
                "max_classification": ceiling,
                "delegated": "act" in claims,
            },
        )
        result = self._policy.evaluate(
            PolicyRequest(
                subject=subject,
                action=action,
                resource=resource,
                risk=RiskLevel(max(1, min(4, risk if isinstance(risk, int) else 1))),
                environment=environment,
            )
        )
        decision, reason, rules = result.decision, result.reason, result.matched_rules
        if int(resource.classification) > ceiling and decision is not Decision.DENY:
            decision, reason, rules = Decision.DENY, "resource exceeds agent classification", ()
        return CallerDecision(
            decision=decision,
            caller=subject.id,
            action=action,
            reason=reason,
            matched_rules=rules,
            policy_version=result.policy_version,
            policy_digest=result.policy_digest,
            mode="local-policy",
        )
