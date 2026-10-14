"""Identity fabric client, inbound agent authorizers, clinician authentication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.policy import PolicyEngine, PolicySet, Resource

from clinical_workflow_orchestration.domain.errors import (
    ForbiddenError,
    IdentityFabricError,
    UnauthorizedError,
)
from clinical_workflow_orchestration.domain.workflow import SignOff, SignOffDecision
from clinical_workflow_orchestration.integrations.identity_fabric import (
    IdentityFabricClient,
    ServiceCredentials,
)
from clinical_workflow_orchestration.orchestration.commit import FabricWriteGate
from clinical_workflow_orchestration.security.agents import (
    FabricAgentAuthorizer,
    LocalAgentAuthorizer,
    bearer_token,
)
from clinical_workflow_orchestration.security.jwks import RemoteJwks, StaticJwks, verify_jwt
from clinical_workflow_orchestration.security.users import UserAuthenticator
from tests.conftest import (
    AGENT_AUDIENCE,
    AGENT_ISSUER,
    CALLER_AGENT,
    ROOT,
    USER_AUDIENCE,
    USER_ISSUER,
    FakeIdp,
    agent_headers,
    clinician_headers,
)
from tests.unit.test_domain_and_persistence import T0, make_workflow

FABRIC = "https://identity.agents.test"
PHI = Resource(type="workflow", id="w1", classification=DataClassification.RESTRICTED)


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine(PolicySet.from_file(ROOT / "policies" / "clinical-workflow.yaml"))


class FakeFabric:
    """Wire shapes of agent-identity-fabric's /oauth2/token, /oauth2/introspect, /v1/authorize."""

    def __init__(self) -> None:
        self.decision = "allow"
        self.active = True
        self.status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(
                self.status, json={"error": "invalid_grant", "error_description": "nope"}
            )
        path = request.url.path
        if path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
            return httpx.Response(
                200,
                json={
                    "access_token": f"cred-{len(self.requests)}",
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "scope": "fhir:write:careplan-bundle",
                },
            )
        if path == "/oauth2/introspect":
            if not self.active:
                return httpx.Response(200, json={"active": False})
            return httpx.Response(
                200, json={"active": True, "sub": CALLER_AGENT, "scope": "workflow:start"}
            )
        if path == "/v1/authorize":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "decision": self.decision,
                    "agent": CALLER_AGENT,
                    "action": body["action"],
                    "resource": f"{body['resource_type']}:{body['resource_id']}",
                    "matched_rules": ["allow-scoped-actions"],
                    "policy_version": "2026.10.0",
                    "policy_digest": "sha256:" + "a" * 64,
                    "reason": "explicit allow",
                    "delegation_chain": [],
                },
            )
        return httpx.Response(404)


def fabric_client(fake: FakeFabric) -> IdentityFabricClient:
    return IdentityFabricClient(
        FABRIC, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )


class TestFabricClient:
    async def test_token_introspect_authorize(self) -> None:
        fake = FakeFabric()
        client = fabric_client(fake)
        issued = await client.token(
            client_id="spiffe://a/agent/b/c", assertion="workload", scopes=frozenset({"b", "a"})
        )
        assert issued.expires_in == 600
        assert parse_qs(fake.requests[0].content.decode())["scope"] == ["a b"]
        introspection = await client.introspect("tok")
        assert introspection.active
        assert introspection.scopes == {"workflow:start"}
        decision = await client.authorize(
            "tok",
            action="workflow:start",
            resource_type="workflow",
            resource_id="w",
            classification=DataClassification.RESTRICTED,
        )
        assert decision.decision is Decision.ALLOW
        sent = json.loads(fake.requests[-1].content)
        assert sent["resource_classification"] == 3
        assert fake.requests[-1].headers["authorization"] == "Bearer tok"

    async def test_errors(self) -> None:
        fake = FakeFabric()
        fake.status = 401
        with pytest.raises(UnauthorizedError, match="nope"):
            await fabric_client(fake).introspect("t")
        fake.status = 503
        with pytest.raises(IdentityFabricError, match="HTTP 503"):
            await fabric_client(fake).introspect("t")

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("x", request=request)

        unreachable = IdentityFabricClient(
            FABRIC, http=httpx.AsyncClient(transport=httpx.MockTransport(down))
        )
        with pytest.raises(IdentityFabricError, match="unreachable"):
            await unreachable.introspect("t")
        garbage = IdentityFabricClient(
            FABRIC,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<"))
            ),
        )
        with pytest.raises(IdentityFabricError, match="non-JSON"):
            await garbage.introspect("t")
        wrong_shape = IdentityFabricClient(
            FABRIC,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"x": 1}))
            ),
        )
        with pytest.raises(IdentityFabricError, match="unexpected identity fabric response"):
            await wrong_shape.introspect("t")
        text_error = IdentityFabricClient(
            FABRIC,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(403, text="no"))
            ),
        )
        with pytest.raises(UnauthorizedError):
            await text_error.introspect("t")

    async def test_service_credentials_cache_and_reread(self, tmp_path: Path) -> None:
        fake = FakeFabric()
        token_file = tmp_path / "token"
        token_file.write_text("workload-token-1\n", encoding="utf-8")
        now = [1000.0]
        credentials = ServiceCredentials(
            fabric_client(fake),
            agent_id="spiffe://a/agent/b/c",
            workload_token_file=token_file,
            clock=lambda: now[0],
        )
        first = await credentials.token()
        assert await credentials.token() == first
        token_file.write_text("workload-token-2", encoding="utf-8")
        now[0] += 560
        assert await credentials.token() != first
        assert parse_qs(fake.requests[-1].content.decode())["assertion"] == ["workload-token-2"]

    async def test_write_gate(self, tmp_path: Path) -> None:
        fake = FakeFabric()
        token_file = tmp_path / "token"
        token_file.write_text("w", encoding="utf-8")
        gate = FabricWriteGate(
            fabric_client(fake),
            ServiceCredentials(fabric_client(fake), agent_id="a", workload_token_file=token_file),
        )
        workflow = make_workflow().model_copy(
            update={
                "signoff": SignOff(
                    decision=SignOffDecision.APPROVE,
                    practitioner="Practitioner/1",
                    subject="s",
                    signed_at=T0,
                    draft_digest="sha256:" + "b" * 64,
                )
            }
        )
        fake.decision = "require_approval"
        await gate.check(workflow)  # satisfied by the clinician sign-off
        environment = json.loads(fake.requests[-1].content)["environment"]
        assert environment == {
            "purpose_of_use": "TREAT",
            "clinician_signed": True,
            "signer": "Practitioner/1",
        }
        fake.decision = "deny"
        with pytest.raises(ForbiddenError, match="identity fabric denied"):
            await gate.check(workflow)


class TestAgentAuthorizers:
    def test_bearer_parsing(self) -> None:
        assert bearer_token("Bearer abc") == "abc"
        for bad in (None, "", "Basic abc", "Bearer   "):
            with pytest.raises(UnauthorizedError):
                bearer_token(bad)

    async def test_fabric_mode(self) -> None:
        fake = FakeFabric()
        authorizer = FabricAgentAuthorizer(fabric_client(fake))
        decision = await authorizer.authorize(
            "Bearer tok",
            action="workflow:start",
            resource=PHI,
            environment={"purpose_of_use": "TREAT"},
        )
        assert decision.decision is Decision.ALLOW
        assert decision.mode == "identity-fabric"
        assert decision.caller == CALLER_AGENT
        fake.active = False
        with pytest.raises(UnauthorizedError, match="not active"):
            await authorizer.authorize(
                "Bearer tok", action="workflow:start", resource=PHI, environment={}
            )

    async def test_local_mode(self, agent_idp: FakeIdp, policy: PolicyEngine) -> None:
        authorizer = LocalAgentAuthorizer(
            StaticJwks(agent_idp.jwks()),
            issuer=AGENT_ISSUER,
            audience=AGENT_AUDIENCE,
            policy=policy,
        )

        async def decide(
            headers: dict[str, str], action: str = "workflow:start", **env: Any
        ) -> Decision:
            environment = {"purpose_of_use": "TREAT", **env}
            result = await authorizer.authorize(
                headers["Authorization"], action=action, resource=PHI, environment=environment
            )
            return result.decision

        assert await decide(agent_headers(agent_idp)) is Decision.ALLOW
        assert await decide(agent_headers(agent_idp, scope="workflow:read")) is Decision.DENY
        assert await decide(agent_headers(agent_idp, scope="workflow:*")) is Decision.ALLOW
        assert await decide(agent_headers(agent_idp), purpose_of_use="MARKETING") is Decision.DENY
        assert await decide(agent_headers(agent_idp, max_cls=1)) is Decision.DENY  # ceiling
        assert (
            await decide(agent_headers(agent_idp, act={"sub": "x"})) is Decision.DENY
        )  # delegated start
        assert (
            await decide(agent_headers(agent_idp, act={"sub": "x"}), action="workflow:read")
            is Decision.ALLOW
        )
        assert await decide(agent_headers(agent_idp), action="careplan:approve") is Decision.DENY
        assert await decide(agent_headers(agent_idp, risk="high", max_cls="3")) is Decision.DENY

    async def test_local_mode_without_verifier_rejects(self, policy: PolicyEngine) -> None:
        authorizer = LocalAgentAuthorizer(None, issuer=None, audience=None, policy=policy)
        with pytest.raises(UnauthorizedError, match="no agent credential verifier"):
            await authorizer.authorize(
                "Bearer x", action="workflow:read", resource=PHI, environment={}
            )

    async def test_local_mode_rejects_foreign_tokens(
        self, agent_idp: FakeIdp, user_idp: FakeIdp, policy: PolicyEngine
    ) -> None:
        authorizer = LocalAgentAuthorizer(
            StaticJwks(agent_idp.jwks()),
            issuer=AGENT_ISSUER,
            audience=AGENT_AUDIENCE,
            policy=policy,
        )
        with pytest.raises(UnauthorizedError):
            await authorizer.authorize(
                clinician_headers(user_idp)["Authorization"],
                action="workflow:read",
                resource=PHI,
                environment={},
            )


class TestUsers:
    async def test_claims_mapping(self, user_idp: FakeIdp) -> None:
        users = UserAuthenticator(
            StaticJwks(user_idp.jwks()), issuer=USER_ISSUER, audience=USER_AUDIENCE
        )
        principal = await users.authenticate(clinician_headers(user_idp)["Authorization"])
        assert principal.roles == {"clinician-approver"}
        assert principal.fhir_user == "Practitioner/cwo-pr1"
        assert principal.token_id
        absolute = user_idp.mint(
            sub="u",
            aud=USER_AUDIENCE,
            roles="not-a-list",
            fhirUser="https://ehr.test/fhir/Practitioner/77",
        )
        other = await users.authenticate(f"Bearer {absolute}")
        assert other.fhir_user == "Practitioner/77"
        assert other.roles == frozenset()
        patient_user = user_idp.mint(sub="u", aud=USER_AUDIENCE, fhirUser="Patient/1")
        assert (await users.authenticate(f"Bearer {patient_user}")).fhir_user is None
        with pytest.raises(UnauthorizedError, match="bearer token required"):
            await users.authenticate(None)
        wrong_audience = user_idp.mint(sub="u", aud="other-api")
        with pytest.raises(UnauthorizedError, match="InvalidAudience"):
            await users.authenticate(f"Bearer {wrong_audience}")


class TestJwks:
    async def test_static(self, user_idp: FakeIdp) -> None:
        resolver = StaticJwks(user_idp.jwks())
        assert await resolver.key_for("entra-1") is not None
        with pytest.raises(UnauthorizedError, match="unknown"):
            await resolver.key_for("nope")

    async def test_remote_cache_refresh_and_rate_limit(self, user_idp: FakeIdp) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=user_idp.jwks())

        now = [0.0]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            remote = RemoteJwks(
                "https://login.hospital.test/jwks",
                client=http,
                ttl_s=100,
                min_refresh_interval_s=10,
                clock=lambda: now[0],
            )
            await remote.key_for("entra-1")
            await remote.key_for("entra-1")
            assert calls["n"] == 1
            with pytest.raises(UnauthorizedError, match="unknown"):
                await remote.key_for("rotated")  # within min interval: no refetch
            assert calls["n"] == 1
            now[0] = 11
            with pytest.raises(UnauthorizedError):
                await remote.key_for("rotated")  # refetch allowed
            assert calls["n"] == 2
            now[0] = 200
            await remote.key_for("entra-1")  # ttl expired
            assert calls["n"] == 3

    async def test_remote_outage_uses_cache(self, user_idp: FakeIdp) -> None:
        healthy = {"ok": True}

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200 if healthy["ok"] else 503, json=user_idp.jwks())

        now = [0.0]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            remote = RemoteJwks("https://x.test/jwks", client=http, ttl_s=5, clock=lambda: now[0])
            await remote.key_for("entra-1")
            healthy["ok"] = False
            now[0] = 100
            assert await remote.key_for("entra-1") is not None  # stale cache tolerated
            with pytest.raises(UnauthorizedError, match="cannot fetch"):
                await remote.key_for("other")

    async def test_verify_jwt_rejections(self, user_idp: FakeIdp) -> None:
        resolver = StaticJwks(user_idp.jwks())
        good = user_idp.mint(sub="s", aud="a")
        claims = await verify_jwt(good, resolver, issuer=USER_ISSUER, audience="a")
        assert claims["sub"] == "s"
        with pytest.raises(UnauthorizedError, match="malformed"):
            await verify_jwt("garbage", resolver, issuer=USER_ISSUER, audience="a")
        unsigned = jwt.encode({"sub": "s"}, key=None, algorithm="none")
        with pytest.raises(UnauthorizedError, match="not accepted"):
            await verify_jwt(unsigned, resolver, issuer=USER_ISSUER, audience="a")
