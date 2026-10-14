"""SMART on FHIR backend-services authorization (client credentials + signed JWT assertion).

Implements the SMART App Launch "Backend Services" profile: the service
authenticates to the EHR authorization server with a short-lived JWT signed
by its own private key (RS384 or ES384), registered with the EHR as a JWKS.
No shared client secret exists anywhere.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from clinical_workflow_orchestration.domain.errors import SmartAuthError

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
SmartAlgorithm = Literal["RS384", "ES384"]

DEFAULT_SCOPES = (
    "system/Patient.read",
    "system/Encounter.read",
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/Medication.read",
    "system/Observation.read",
    "system/AllergyIntolerance.read",
    "system/CarePlan.read",
    "system/CarePlan.write",
    "system/Task.read",
    "system/Task.write",
    "system/Provenance.write",
)


def load_signing_key(pem: bytes, algorithm: SmartAlgorithm) -> Any:
    """Load a PEM private key and check it fits the algorithm SMART requires."""
    key = serialization.load_pem_private_key(pem, password=None)
    if algorithm == "RS384":
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("RS384 requires an RSA private key of at least 2048 bits")
    elif not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp384r1":
        raise ValueError("ES384 requires an EC P-384 private key")
    return key


class SmartBackendAuth:
    def __init__(
        self,
        *,
        client_id: str,
        private_key: Any,
        key_id: str,
        algorithm: SmartAlgorithm,
        fhir_base_url: str,
        http: httpx.AsyncClient,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        token_url: str | None = None,
        clock: Callable[[], float] = time.time,
        assertion_ttl_s: int = 300,
        refresh_margin_s: int = 60,
    ) -> None:
        self._client_id = client_id
        self._key = private_key
        self._kid = key_id
        self._alg = algorithm
        self._base = fhir_base_url.rstrip("/")
        self._http = http
        self._scopes = scopes
        self._token_url = token_url
        self._clock = clock
        self._assertion_ttl = assertion_ttl_s
        self._margin = refresh_margin_s
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token_endpoint(self) -> str:
        if self._token_url is None:
            try:
                response = await self._http.get(
                    f"{self._base}/.well-known/smart-configuration",
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SmartAuthError(f"SMART discovery failed: {type(exc).__name__}") from exc
            endpoint = document.get("token_endpoint") if isinstance(document, dict) else None
            if not isinstance(endpoint, str) or not endpoint:
                raise SmartAuthError("SMART configuration has no token_endpoint")
            self._token_url = endpoint
        return self._token_url

    def client_assertion(self, token_url: str) -> str:
        now = int(self._clock())
        claims = {
            "iss": self._client_id,
            "sub": self._client_id,
            "aud": token_url,
            "jti": secrets.token_urlsafe(24),
            "iat": now,
            "exp": now + self._assertion_ttl,
        }
        return jwt.encode(
            claims, self._key, algorithm=self._alg, headers={"kid": self._kid, "typ": "JWT"}
        )

    def invalidate(self) -> None:
        """Drop the cached access token (called after the FHIR server answers 401)."""
        self._token = None
        self._expires_at = 0.0

    async def access_token(self) -> str:
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at - self._margin:
                return self._token
            token_url = await self.token_endpoint()
            form = {
                "grant_type": "client_credentials",
                "scope": " ".join(self._scopes),
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": self.client_assertion(token_url),
            }
            try:
                response = await self._http.post(
                    token_url, data=form, headers={"Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                raise SmartAuthError(f"token endpoint unreachable: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise SmartAuthError(
                    f"token endpoint returned HTTP {response.status_code}: {_oauth_error(response)}"
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise SmartAuthError("token response is not JSON") from exc
            token = body.get("access_token") if isinstance(body, dict) else None
            expires_in = body.get("expires_in", 300) if isinstance(body, dict) else 300
            if not isinstance(token, str) or not token:
                raise SmartAuthError("token response has no access_token")
            if not isinstance(expires_in, int) or expires_in <= 0:
                expires_in = 300
            self._token = token
            self._expires_at = self._clock() + expires_in
            return token


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])[:200]
    return response.reason_phrase or "error"
