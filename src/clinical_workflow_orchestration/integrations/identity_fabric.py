"""Typed client for the agent identity fabric (github.com/kogunlowo123/agent-identity-fabric).

* ``token``      RFC 7523 jwt-bearer: exchange this service's workload token
                 for a short-lived agent credential.
* ``introspect`` RFC 7662: is a caller's credential active, and whose is it.
* ``authorize``  policy decision for an action by the credential's agent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, ValidationError

from clinical_workflow_orchestration.domain.errors import IdentityFabricError, UnauthorizedError

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class IssuedCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str
    token_type: str
    expires_in: int
    scope: str = ""


class Introspection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    jti: str | None = None
    act: dict[str, Any] | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset((self.scope or "").split())


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...] = ()
    policy_version: str = ""
    policy_digest: str = ""
    reason: str = ""
    delegation_chain: tuple[str, ...] = ()


class IdentityFabricClient:
    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def token(
        self, *, client_id: str, assertion: str, scopes: frozenset[str] | None = None
    ) -> IssuedCredential:
        form = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scopes:
            form["scope"] = " ".join(sorted(scopes))
        body = await self._call("POST", "/oauth2/token", data=form)
        return _parse(IssuedCredential, body)

    async def introspect(self, token: str) -> Introspection:
        body = await self._call("POST", "/oauth2/introspect", data={"token": token})
        return _parse(Introspection, body)

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        classification: DataClassification,
        attributes: dict[str, Any] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> FabricDecision:
        body = await self._call(
            "POST",
            "/v1/authorize",
            json={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_classification": int(classification),
                "resource_attributes": attributes or {},
                "environment": environment or {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        return _parse(FabricDecision, body)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", data=data, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise IdentityFabricError(
                f"identity fabric {path} unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code in {400, 401, 403}:
            raise UnauthorizedError(f"identity fabric rejected {path}: {_error(response)}")
        if response.status_code >= 300:
            raise IdentityFabricError(
                f"identity fabric {path} returned HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise IdentityFabricError(f"identity fabric {path} returned non-JSON") from exc


def _parse[M: BaseModel](model: type[M], body: Any) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise IdentityFabricError(
            f"unexpected identity fabric response for {model.__name__}"
        ) from exc


def _error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("error") or "error")[:200]
    return "error"


class ServiceCredentials:
    """This service's own agent credential, obtained with its projected workload token."""

    def __init__(
        self,
        fabric: IdentityFabricClient,
        *,
        agent_id: str,
        workload_token_file: Path,
        clock: Callable[[], float] = time.time,
        refresh_margin_s: int = 60,
    ) -> None:
        self._fabric = fabric
        self._agent_id = agent_id
        self._token_file = workload_token_file
        self._clock = clock
        self._margin = refresh_margin_s
        self._credential: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._credential is not None and self._clock() < self._expires_at - self._margin:
                return self._credential
            # Kubernetes rotates the projected token file; read it fresh every time.
            assertion = (await asyncio.to_thread(self._token_file.read_text, "utf-8")).strip()
            issued = await self._fabric.token(client_id=self._agent_id, assertion=assertion)
            self._credential = issued.access_token
            self._expires_at = self._clock() + issued.expires_in
            return issued.access_token
