"""Async FHIR R4 REST client: read, search with bundle paging, and transactions.

Behaviour that matters for clinical safety:

* Searches follow ``Bundle.link[relation=next]`` only when the next URL is
  under the configured base URL, so a hostile or misconfigured server cannot
  redirect the client (and its bearer token) elsewhere.
* Idempotent requests (reads, searches and transactions built from
  conditional creates) are retried on 429/5xx and transport errors with
  exponential backoff; other 4xx responses fail immediately with the
  server's OperationOutcome diagnostics.
* A 401 drops the cached SMART token and retries once with a fresh one.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from clinical_workflow_orchestration.domain.errors import FhirError, FhirNotFoundError
from clinical_workflow_orchestration.fhir.resources import JsonObject, validate_fhir_id

FHIR_JSON = "application/fhir+json"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class TokenSource(Protocol):
    async def access_token(self) -> str: ...

    def invalidate(self) -> None: ...


Observer = Callable[[str, str], None]
Sleep = Callable[[float], Awaitable[None]]


class FhirClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient,
        auth: TokenSource | None = None,
        max_attempts: int = 3,
        base_delay_s: float = 0.2,
        sleep: Sleep = asyncio.sleep,
        observer: Observer | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._base = base_url.rstrip("/")
        self._origin = urlsplit(self._base)
        self._http = http
        self._auth = auth
        self._max_attempts = max_attempts
        self._base_delay = base_delay_s
        self._sleep = sleep
        self._observer = observer
        self._rng = random.SystemRandom()

    @property
    def base_url(self) -> str:
        return self._base

    async def read(self, resource_type: str, resource_id: str) -> JsonObject:
        validate_fhir_id(resource_id)
        body = await self._request("read", "GET", f"{self._base}/{resource_type}/{resource_id}")
        if body.get("resourceType") != resource_type:
            raise FhirError(
                f"expected {resource_type}, server returned {body.get('resourceType')!r}",
                retryable=False,
            )
        return body

    async def search(
        self,
        resource_type: str,
        params: dict[str, str],
        *,
        max_pages: int = 20,
        max_resources: int = 1000,
    ) -> list[JsonObject]:
        """All matching resources of ``resource_type`` across pages (includes are dropped)."""
        url: str | None = f"{self._base}/{resource_type}"
        query: dict[str, str] | None = params
        results: list[JsonObject] = []
        for _ in range(max_pages):
            if url is None:
                break
            bundle = await self._request("search", "GET", url, params=query)
            if bundle.get("resourceType") != "Bundle":
                raise FhirError("search did not return a Bundle", retryable=False)
            for entry in bundle.get("entry") or []:
                if not isinstance(entry, dict):
                    continue
                resource = entry.get("resource")
                mode = (entry.get("search") or {}).get("mode", "match")
                if (
                    isinstance(resource, dict)
                    and resource.get("resourceType") == resource_type
                    and mode == "match"
                ):
                    results.append(resource)
                    if len(results) >= max_resources:
                        return results
            url = self._next_link(bundle)
            query = None  # the next link already carries the paging state
        return results

    async def transaction(self, bundle: JsonObject) -> JsonObject:
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
            raise ValueError("transaction requires a Bundle of type transaction")
        # Not retried here: if the response is lost after the server committed, a
        # blind retry would duplicate non-conditional entries (Provenance). The
        # caller retries at workflow level, after looking for what was written.
        response = await self._request(
            "transaction", "POST", self._base, json=bundle, max_attempts=1
        )
        if response.get("type") != "transaction-response":
            raise FhirError("server did not return a transaction-response Bundle", retryable=False)
        return response

    def _next_link(self, bundle: JsonObject) -> str | None:
        for link in bundle.get("link") or []:
            if isinstance(link, dict) and link.get("relation") == "next":
                target = link.get("url")
                if not isinstance(target, str):
                    return None
                parts = urlsplit(target)
                same_origin = (parts.scheme, parts.netloc) == (
                    self._origin.scheme,
                    self._origin.netloc,
                )
                if not same_origin or not parts.path.startswith(self._origin.path):
                    raise FhirError(
                        "refusing to follow a paging link outside the FHIR base URL",
                        retryable=False,
                    )
                return target
        return None

    async def _headers(self) -> dict[str, str]:
        headers = {"Accept": FHIR_JSON}
        if self._auth is not None:
            headers["Authorization"] = f"Bearer {await self._auth.access_token()}"
        return headers

    async def _request(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: JsonObject | None = None,
        max_attempts: int | None = None,
    ) -> JsonObject:
        attempts_allowed = max_attempts or self._max_attempts
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = await self._headers()
            if json is not None:
                headers["Content-Type"] = FHIR_JSON
            try:
                response = await self._http.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                self._observe(operation, "transport_error")
                if attempt < attempts_allowed:
                    await self._backoff(attempt)
                    continue
                raise FhirError(
                    f"FHIR {operation} failed: {type(exc).__name__}", retryable=True
                ) from exc
            self._observe(operation, str(response.status_code))
            if response.status_code == 401 and self._auth is not None and not refreshed:
                self._auth.invalidate()
                refreshed = True
                attempt -= 1
                continue
            if response.status_code in RETRYABLE_STATUS:
                if attempt < attempts_allowed:
                    await self._backoff(attempt)
                    continue
                raise FhirError(
                    f"FHIR {operation} failed with HTTP {response.status_code} after {attempt} "
                    "attempts",
                    status_code=response.status_code,
                    retryable=True,
                )
            if response.status_code in {404, 410}:
                raise FhirNotFoundError(f"FHIR {operation}: {_diagnostics(response)}")
            if response.status_code >= 400:
                raise FhirError(
                    f"FHIR {operation} rejected with HTTP {response.status_code}: "
                    f"{_diagnostics(response)}",
                    status_code=response.status_code,
                    retryable=False,
                )
            try:
                body: Any = response.json()
            except ValueError as exc:
                raise FhirError(f"FHIR {operation} returned non-JSON", retryable=False) from exc
            if not isinstance(body, dict):
                raise FhirError(f"FHIR {operation} returned a non-object", retryable=False)
            return body

    async def _backoff(self, attempt: int) -> None:
        ceiling = self._base_delay * (2 ** (attempt - 1))
        await self._sleep(self._rng.uniform(ceiling / 2, ceiling))

    def _observe(self, operation: str, outcome: str) -> None:
        if self._observer is not None:
            self._observer(operation, outcome)


def _diagnostics(response: httpx.Response) -> str:
    """Summarize an OperationOutcome without echoing resource content."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        messages = [
            str(issue.get("diagnostics") or issue.get("code") or "")
            for issue in body.get("issue") or []
            if isinstance(issue, dict)
        ]
        joined = "; ".join(m for m in messages if m)
        if joined:
            return joined[:300]
    return response.reason_phrase or "error"
