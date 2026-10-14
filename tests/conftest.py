"""Shared fixtures.

* ``FakeFhirServer``: an in-memory FHIR R4 server (read, search with paging,
  transactions with conditional create, SMART backend-services token
  endpoint) behind ``httpx.MockTransport``, loaded with the synthetic
  discharge dataset used by the compose stack.
* ``FakeModel``: Azure OpenAI and vLLM chat-completions endpoints that
  replay scripted care-plan drafts and clinical reviews.
* ``FakeIdp``: ES256 OIDC issuers for clinicians and for agent credentials.

No test needs network access, API keys or cloud credentials.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from clinical_workflow_orchestration.api.app import create_app
from clinical_workflow_orchestration.config import Settings
from clinical_workflow_orchestration.runtime.container import Container, build_container
from clinical_workflow_orchestration.security.jwks import StaticJwks

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "deployments" / "docker" / "fhir-seed" / "synthetic-heart-failure-discharge.json"
FHIR_BASE = "https://fhir.hospital.test/fhir"
TOKEN_URL = "https://auth.hospital.test/oauth2/token"
USER_ISSUER = "https://login.hospital.test/tenant"
USER_AUDIENCE = "api://clinical-workflow-orchestration"
AGENT_ISSUER = "https://identity.agents.test"
AGENT_AUDIENCE = "clinical-workflow-orchestration"
ORCHESTRATOR_ID = "spiffe://agents.test/agent/clinical/orchestrator"
CALLER_AGENT = "spiffe://agents.test/agent/clinical/discharge-planner"
AOAI = "https://aoai.hospital.test"
VLLM = "http://vllm.cwo.test:8000"
PROVIDER_ENV = {
    "CWO_AZURE_OPENAI_MODEL": "gpt-4o",
    "CWO_AZURE_OPENAI_INPUT_COST_PER_MTOK": "2.5",
    "CWO_AZURE_OPENAI_OUTPUT_COST_PER_MTOK": "10",
    "CWO_AZURE_REGION": "eastus2",
    "CWO_AZURE_OPENAI_ENDPOINT": AOAI,
    "CWO_AZURE_OPENAI_DEPLOYMENT": "care-plans",
    "CWO_AZURE_OPENAI_API_KEY": "test-azure-key",
    "CWO_SELF_HOSTED_MODEL": "llama-3.1-70b-instruct",
    "CWO_SELF_HOSTED_LLM_URL": VLLM,
}
AGENT_SCOPES = "workflow:start workflow:read workflow:list medication:check"
PRACTITIONER = "Practitioner/cwo-pr1"

JsonObject = dict[str, Any]


# ----------------------------------------------------------------- identity


@dataclass
class FakeIdp:
    """Minimal OIDC issuer: ES256 keypair plus JWKS."""

    issuer: str
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> JsonObject:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str, ttl: int = 300, **extra: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": sub,
            "aud": aud,
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


@dataclass
class FakeClock:
    current: datetime = field(default_factory=lambda: datetime(2026, 10, 14, 9, 0, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def clinician_headers(
    idp: FakeIdp, *roles: str, fhir_user: str | None = PRACTITIONER
) -> dict[str, str]:
    extra: dict[str, Any] = {"roles": list(roles or ("clinician-approver",))}
    if fhir_user is not None:
        extra["fhirUser"] = fhir_user
    token = idp.mint(sub="avery.synthetic@hospital.test", aud=USER_AUDIENCE, **extra)
    return {"Authorization": f"Bearer {token}"}


def coordinator_headers(idp: FakeIdp) -> dict[str, str]:
    token = idp.mint(
        sub="casey.coordinator@hospital.test", aud=USER_AUDIENCE, roles=["care-coordinator"]
    )
    return {"Authorization": f"Bearer {token}"}


def agent_headers(idp: FakeIdp, scope: str = AGENT_SCOPES, **extra: Any) -> dict[str, str]:
    extra.setdefault("max_cls", 3)
    token = idp.mint(sub=CALLER_AGENT, aud=AGENT_AUDIENCE, scope=scope, **extra)
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- FHIR


def _references(node: Any) -> Iterator[JsonObject]:
    if isinstance(node, dict):
        if isinstance(node.get("reference"), str):
            yield node
        for value in node.values():
            yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


def _subject_refs(resource: JsonObject) -> set[str]:
    refs = set()
    for key in ("subject", "patient", "for"):
        value = resource.get(key)
        if isinstance(value, dict) and isinstance(value.get("reference"), str):
            refs.add(value["reference"])
    return refs


@dataclass
class FakeFhirServer:
    base: str = FHIR_BASE
    page_size: int = 2
    store: dict[str, dict[str, JsonObject]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    transactions: list[JsonObject] = field(default_factory=list)
    fail_with: list[int] = field(default_factory=list)
    smart_public_key: Any = None
    require_token: bool = False
    issued_tokens: list[str] = field(default_factory=list)
    next_link_override: str | None = None
    _pages: dict[str, list[JsonObject]] = field(default_factory=dict)
    _counter: int = 0

    def load(self, bundle: JsonObject) -> None:
        for entry in bundle["entry"]:
            resource = copy.deepcopy(entry["resource"])
            resource.setdefault("meta", {})["versionId"] = "1"
            self.store.setdefault(resource["resourceType"], {})[resource["id"]] = resource

    def put(self, resource: JsonObject) -> None:
        self.store.setdefault(resource["resourceType"], {})[resource["id"]] = resource

    def all(self, resource_type: str) -> list[JsonObject]:
        return list(self.store.get(resource_type, {}).values())

    # -- handler -------------------------------------------------------

    # A test double routing a whole FHIR REST surface; one return per route.
    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        self.requests.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            return self._token(request)
        if url == f"{self.base}/.well-known/smart-configuration":
            return httpx.Response(200, json={"token_endpoint": TOKEN_URL})
        if not url.startswith(self.base):
            return httpx.Response(404, json={"resourceType": "OperationOutcome"})
        if self.fail_with:
            return httpx.Response(self.fail_with.pop(0), json=_outcome("injected failure"))
        if self.require_token:
            auth = request.headers.get("authorization", "")
            if auth[7:] not in self.issued_tokens:
                return httpx.Response(401, json=_outcome("invalid token"))
        parts = urlsplit(url)
        path = parts.path[len(urlsplit(self.base).path) :].strip("/")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if request.method == "POST" and path == "":
            return self._transaction(json.loads(request.content))
        if request.method == "GET" and path == "" and "_getpages" in query:
            return self._page(query["_getpages"])
        segments = path.split("/")
        if request.method == "GET" and len(segments) == 2:
            resource = self.store.get(segments[0], {}).get(segments[1])
            if resource is None:
                return httpx.Response(404, json=_outcome(f"{path} not found"))
            return httpx.Response(200, json=resource)
        if request.method == "GET" and len(segments) == 1:
            return self._search(segments[0], query)
        return httpx.Response(400, json=_outcome("unsupported"))

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        try:
            claims = jwt.decode(
                form["client_assertion"],
                self.smart_public_key,
                algorithms=["RS384", "ES384"],
                audience=TOKEN_URL,
            )
        except (KeyError, jwt.PyJWTError):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") != "client_credentials" or claims["iss"] != claims["sub"]:
            return httpx.Response(400, json={"error": "invalid_request"})
        token = uuid.uuid4().hex
        self.issued_tokens.append(token)
        return httpx.Response(
            200, json={"access_token": token, "token_type": "bearer", "expires_in": 300}
        )

    # One return per supported FHIR search parameter.
    def _matches(self, resource: JsonObject, key: str, value: str) -> bool:  # noqa: PLR0911
        if key in {"_count", "_sort"}:
            return True
        if key == "patient":
            return f"Patient/{value}" in _subject_refs(resource)
        if key == "clinical-status":
            codes = [
                c.get("code") for c in (resource.get("clinicalStatus") or {}).get("coding", [])
            ]
            return any(c in value.split(",") for c in codes)
        if key == "status":
            return resource.get("status") in value.split(",")
        if key == "category":
            return any(
                coding.get("code") == value
                for category in resource.get("category") or []
                for coding in (category.get("coding") or [] if isinstance(category, dict) else [])
            )
        if key == "identifier":
            system, _, ident = value.partition("|")
            return any(
                i.get("system") == system and i.get("value") == ident
                for i in resource.get("identifier") or []
            )
        if key == "based-on":
            return any(r.get("reference") == value for r in resource.get("basedOn") or [])
        if key == "target":
            return any(r.get("reference") == value for r in resource.get("target") or [])
        return False

    def _search(self, resource_type: str, query: dict[str, str]) -> httpx.Response:
        matches = [
            r
            for r in self.all(resource_type)
            if all(self._matches(r, k, v) for k, v in query.items())
        ]
        if query.get("_sort") == "-date":
            matches.sort(key=lambda r: r.get("effectiveDateTime", ""), reverse=True)
        return self._bundle(matches)

    def _bundle(self, matches: list[JsonObject]) -> httpx.Response:
        page, rest = matches[: self.page_size], matches[self.page_size :]
        bundle: JsonObject = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(matches),
            "entry": [{"resource": r, "search": {"mode": "match"}} for r in page],
            "link": [],
        }
        if rest:
            token = uuid.uuid4().hex
            self._pages[token] = rest
            bundle["link"].append(
                {
                    "relation": "next",
                    "url": self.next_link_override or f"{self.base}?_getpages={token}",
                }
            )
        return httpx.Response(200, json=bundle, headers={"content-type": "application/fhir+json"})

    def _page(self, token: str) -> httpx.Response:
        return self._bundle(self._pages.pop(token, []))

    def _transaction(self, bundle: JsonObject) -> httpx.Response:
        if bundle.get("type") != "transaction":
            return httpx.Response(400, json=_outcome("expected transaction"))
        self.transactions.append(copy.deepcopy(bundle))
        urns: dict[str, str] = {}
        plans: list[tuple[JsonObject, str, str, bool]] = []
        for entry in bundle["entry"]:
            resource = copy.deepcopy(entry["resource"])
            rtype = resource["resourceType"]
            request = entry["request"]
            existing = None
            if request["method"] == "POST" and request.get("ifNoneExist"):
                key, _, value = request["ifNoneExist"].partition("=")
                found = [r for r in self.all(rtype) if self._matches(r, key, value)]
                existing = found[0] if found else None
            if existing is not None:
                rid, created = existing["id"], False
            elif request["method"] == "PUT":
                rid, created = request["url"].split("/")[1], True
            else:
                self._counter += 1
                rid, created = f"{rtype.lower()}-{self._counter}", True
            urns[entry["fullUrl"]] = f"{rtype}/{rid}"
            plans.append((resource, rtype, rid, created))
        out = []
        for resource, rtype, rid, created in plans:
            for ref in _references(resource):
                if ref["reference"] in urns:
                    ref["reference"] = urns[ref["reference"]]
            if created:
                resource["id"] = rid
                resource["meta"] = {"versionId": "1"}
                self.put(resource)
            out.append(
                {
                    "response": {
                        "status": "201 Created" if created else "200 OK",
                        "location": f"{rtype}/{rid}/_history/1",
                    }
                }
            )
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response", "entry": out}
        )


def _outcome(message: str) -> JsonObject:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": message}],
    }


# -------------------------------------------------------------------- model


SAFE_DRAFT: JsonObject = {
    "title": "Post-discharge heart failure care plan",
    "goals": ["Avoid readmission in the next 30 days", "Potassium back within normal range"],
    "activities": [
        {
            "kind": "follow-up",
            "description": "Heart failure clinic visit",
            "due_in_days": 5,
            "priority": "urgent",
            "reason_refs": ["cwo-c1"],
        },
        {
            "kind": "lab",
            "description": "Repeat serum potassium",
            "code": "2823-3",
            "due_in_days": 2,
            "priority": "urgent",
            "reason_refs": ["cwo-o1"],
        },
        {
            "kind": "follow-up",
            "description": "Primary care review of diabetes and blood pressure",
            "due_in_days": 21,
            "reason_refs": ["cwo-c2", "cwo-c3"],
        },
        {
            "kind": "monitoring",
            "description": "Daily weights; call if gain over 2 kg in 3 days",
            "due_in_days": 1,
            "reason_refs": ["cwo-c1"],
        },
        {
            "kind": "medication",
            "description": "Continue lisinopril; recheck potassium before any dose change",
            "due_in_days": 0,
            "medication": {
                "action": "continue",
                "display": "lisinopril 10 MG Oral Tablet",
                "existing_request_id": "cwo-m1",
            },
        },
        {
            "kind": "education",
            "description": "Low-sodium diet and fluid guidance",
            "due_in_days": 3,
        },
    ],
    "rationale": "Recent heart failure admission with hyperkalaemia on an ACE inhibitor.",
}

RISKY_DRAFT: JsonObject = {
    "title": "Post-discharge plan",
    "goals": ["Treat infection"],
    "activities": [
        {
            "kind": "medication",
            "description": "Start amoxicillin for suspected infection",
            "due_in_days": 0,
            "medication": {
                "action": "start",
                "display": "amoxicillin 500 MG Oral Capsule",
                "rxnorm_code": "308191",
            },
        },
        {
            "kind": "medication",
            "description": "Add losartan for blood pressure",
            "due_in_days": 0,
            "medication": {"action": "start", "display": "losartan potassium 50 MG Oral Tablet"},
        },
        {
            "kind": "follow-up",
            "description": "Primary care visit",
            "due_in_days": 30,
            "reason_refs": ["cwo-c3"],
        },
    ],
    "rationale": "Draft that the validation rules must reject.",
}

DEFAULT_REVIEW: JsonObject = {
    "concerns": [
        {
            "severity": "warning",
            "category": "monitoring",
            "message": "Consider a weight threshold that triggers same-day contact.",
            "activity_index": 3,
        }
    ],
    "summary": "Plan addresses the admission diagnosis and the abnormal potassium.",
}


@dataclass
class FakeModel:
    draft: JsonObject = field(default_factory=lambda: copy.deepcopy(SAFE_DRAFT))
    review: JsonObject = field(default_factory=lambda: copy.deepcopy(DEFAULT_REVIEW))
    raw_replies: list[str] = field(default_factory=list)
    fail_primary: int = 0
    fail_all: bool = False
    fail_after_calls: int | None = None
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        host = request.url.host
        self.calls.append((host, payload))
        if self.fail_after_calls is not None and len(self.calls) > self.fail_after_calls:
            return httpx.Response(503, json={"error": {"message": "capacity exhausted"}})
        if self.fail_all or (host == urlsplit(AOAI).hostname and self.fail_primary > 0):
            if host == urlsplit(AOAI).hostname and self.fail_primary > 0:
                self.fail_primary -= 1
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        system = next(m["content"] for m in payload["messages"] if m["role"] == "system")
        if self.raw_replies:
            text = self.raw_replies.pop(0)
        elif "care-coordination agent" in system:
            text = json.dumps(self.draft)
        else:
            text = json.dumps(self.review)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "model": payload.get("model", "gpt-4o"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1800, "completion_tokens": 420},
            },
        )


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def fhir() -> FakeFhirServer:
    server = FakeFhirServer()
    server.load(json.loads(SEED.read_text(encoding="utf-8")))
    return server


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def user_idp() -> FakeIdp:
    return FakeIdp(USER_ISSUER, kid="entra-1")


@pytest.fixture
def agent_idp() -> FakeIdp:
    return FakeIdp(AGENT_ISSUER, kid="fabric-1")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db = os.environ.get("CWO_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cwo.db'}")
    return Settings(
        environment="test",
        public_url="https://cwo.hospital.test",
        database_url=db,
        fhir_base_url=FHIR_BASE,
        dev_ephemeral_keys=True,
        audit_log_path=tmp_path / "audit.jsonl",
        agent_id=ORCHESTRATOR_ID,
        user_issuer=USER_ISSUER,
        user_audience=USER_AUDIENCE,
        user_jwks_url="https://login.hospital.test/jwks",
        agent_token_issuer=AGENT_ISSUER,
        agent_token_audience=AGENT_AUDIENCE,
        agent_jwks_url="https://identity.agents.test/.well-known/jwks.json",
        providers_file=ROOT / "configs" / "providers.yaml",
        policy_file=ROOT / "policies" / "clinical-workflow.yaml",
        sweep_interval_seconds=600,
        retry_base_seconds=30,
    )


ContainerBuilder = Callable[..., Any]


# Fixture parameters are injected by pytest, never passed positionally by callers.
@pytest.fixture
def build(  # noqa: PLR0917
    settings: Settings,
    fhir: FakeFhirServer,
    model: FakeModel,
    user_idp: FakeIdp,
    agent_idp: FakeIdp,
    clock: FakeClock,
) -> ContainerBuilder:
    async def factory(**overrides: Any) -> Container:
        options: dict[str, Any] = {
            "clock": clock,
            "http": httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler)),
            "model_transport": httpx.MockTransport(model.handler),
            "env": PROVIDER_ENV,
            "user_resolver": StaticJwks(user_idp.jwks()),
            "agent_resolver": StaticJwks(agent_idp.jwks()),
        }
        options.update(overrides)
        target = options.pop("settings", settings)
        return await build_container(target, **options)

    return factory


@pytest.fixture
async def container(build: ContainerBuilder) -> AsyncIterator[Container]:
    built: Container = await build()
    yield built
    await built.aclose()


@pytest.fixture
def client(build: ContainerBuilder) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""
    with TestClient(create_app(build)) as test_client:
        yield test_client


def wait_for(
    client: TestClient,
    workflow_id: str,
    headers: dict[str, str],
    states: set[str],
    timeout_s: float = 15.0,
) -> JsonObject:
    deadline = time.monotonic() + timeout_s
    body: JsonObject = {}
    while time.monotonic() < deadline:
        body = client.get(f"/v1/workflows/{workflow_id}", headers=headers).json()
        if body.get("state") in states:
            return body
        time.sleep(0.02)
    raise AssertionError(f"workflow stuck in {body.get('state')}: {body.get('failure')}")
