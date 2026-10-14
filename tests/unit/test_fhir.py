"""FHIR R4 client, SMART backend-services auth, resource helpers and bundle builders."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from clinical_workflow_orchestration.domain.errors import FhirError, SmartAuthError
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.fhir.resources import (
    CodeableConcept,
    first_codeable_concept,
    location_reference,
    parse_codeable_concept,
    reference_id,
    status_code,
    validate_fhir_id,
)
from clinical_workflow_orchestration.fhir.smart import SmartBackendAuth, load_signing_key
from tests.conftest import FHIR_BASE, TOKEN_URL, FakeFhirServer


async def _no_sleep(_: float) -> None:
    return None


def fhir_client(server: FakeFhirServer, **kwargs: Any) -> FhirClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return FhirClient(FHIR_BASE, http=http, sleep=_no_sleep, **kwargs)


class TestResources:
    def test_ids_are_validated(self) -> None:
        assert validate_fhir_id("cwo-p1.2") == "cwo-p1.2"
        for bad in ("", "a/b", "a?b=1", "x" * 65, "../Patient"):
            with pytest.raises(ValueError, match="invalid FHIR id"):
                validate_fhir_id(bad)

    def test_reference_parsing(self) -> None:
        assert reference_id({"reference": "Patient/123"}, "Patient") == "123"
        assert reference_id("https://ehr.test/fhir/Patient/9/_history/2", "Patient") == "9"
        assert reference_id({"reference": "Group/1"}, "Patient") is None
        assert reference_id(None, "Patient") is None
        assert location_reference("CarePlan/7/_history/1") == "CarePlan/7"
        assert location_reference("https://ehr.test/fhir/Task/3/_history/1") == "Task/3"
        with pytest.raises(ValueError, match="unexpected FHIR location"):
            location_reference("bogus")

    def test_codeable_concepts(self) -> None:
        concept = parse_codeable_concept(
            {"coding": [{"system": "s", "code": "c", "display": "D"}, "junk"], "text": None}
        )
        assert concept.label == "D"
        assert concept.has_code("s", "c")
        assert concept.as_fhir() == {"coding": [{"system": "s", "code": "c", "display": "D"}]}
        assert parse_codeable_concept("nope") == CodeableConcept()
        assert CodeableConcept().label == "unspecified"
        assert parse_codeable_concept({"coding": [{"code": "x"}]}).label == "x"
        assert first_codeable_concept([{"text": "first"}, {"text": "second"}]).label == "first"
        assert status_code({"coding": [{"code": "active"}]}) == "active"
        assert status_code({"text": "on hold"}) == "on hold"


class TestClient:
    async def test_read_and_type_check(self, fhir: FakeFhirServer) -> None:
        client = fhir_client(fhir)
        patient = await client.read("Patient", "cwo-p1")
        assert patient["gender"] == "female"
        fhir.put({"resourceType": "Group", "id": "g1"})
        fhir.store["Patient"]["confused"] = {"resourceType": "Group", "id": "confused"}
        with pytest.raises(FhirError, match="expected Patient"):
            await client.read("Patient", "confused")
        with pytest.raises(ValueError, match="invalid FHIR id"):
            await client.read("Patient", "../../admin")

    async def test_search_follows_pages(self, fhir: FakeFhirServer) -> None:
        client = fhir_client(fhir)
        results = await client.search("Observation", {"patient": "cwo-p1"})
        assert len(results) == 4  # page size 2: two pages
        assert sum(1 for r in fhir.requests if "_getpages" in str(r.url)) == 1
        capped = await client.search("Observation", {"patient": "cwo-p1"}, max_resources=3)
        assert len(capped) == 3
        one_page = await client.search("Observation", {"patient": "cwo-p1"}, max_pages=1)
        assert len(one_page) == 2

    async def test_search_refuses_foreign_next_link(self, fhir: FakeFhirServer) -> None:
        fhir.next_link_override = "https://attacker.test/fhir?_getpages=x"
        with pytest.raises(FhirError, match="outside the FHIR base URL"):
            await fhir_client(fhir).search("Observation", {"patient": "cwo-p1"})

    async def test_retries_transient_errors(self, fhir: FakeFhirServer) -> None:
        outcomes: list[tuple[str, str]] = []
        fhir.fail_with = [503, 429]
        client = fhir_client(fhir, observer=lambda op, out: outcomes.append((op, out)))
        assert (await client.read("Patient", "cwo-p1"))["id"] == "cwo-p1"
        assert outcomes == [("read", "503"), ("read", "429"), ("read", "200")]

    async def test_gives_up_after_max_attempts(self, fhir: FakeFhirServer) -> None:
        fhir.fail_with = [503, 503, 503]
        with pytest.raises(FhirError) as caught:
            await fhir_client(fhir).read("Patient", "cwo-p1")
        assert caught.value.retryable
        assert caught.value.status_code == 503

    async def test_client_errors_carry_diagnostics(self, fhir: FakeFhirServer) -> None:
        with pytest.raises(FhirError, match="Patient/missing not found") as caught:
            await fhir_client(fhir).read("Patient", "missing")
        assert caught.value.retryable is False

    async def test_transport_errors(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        client = FhirClient(FHIR_BASE, http=http, sleep=_no_sleep, max_attempts=2)
        with pytest.raises(FhirError, match="ConnectError") as caught:
            await client.read("Patient", "p")
        assert caught.value.retryable

    async def test_non_json_and_non_object_bodies(self) -> None:
        bodies = iter([httpx.Response(200, text="<html>"), httpx.Response(200, json=[1, 2])])
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(bodies)))
        client = FhirClient(FHIR_BASE, http=http, sleep=_no_sleep)
        with pytest.raises(FhirError, match="non-JSON"):
            await client.read("Patient", "p")
        with pytest.raises(FhirError, match="non-object"):
            await client.read("Patient", "p")

    async def test_search_requires_bundle(self) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"resourceType": "Patient"})
            )
        )
        with pytest.raises(FhirError, match="did not return a Bundle"):
            await FhirClient(FHIR_BASE, http=http).search("Patient", {})

    async def test_transaction_validation(self, fhir: FakeFhirServer) -> None:
        client = fhir_client(fhir)
        with pytest.raises(ValueError, match="transaction"):
            await client.transaction({"resourceType": "Bundle", "type": "batch"})
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"resourceType": "Bundle", "type": "batch"})
            )
        )
        with pytest.raises(FhirError, match="transaction-response"):
            await FhirClient(FHIR_BASE, http=http).transaction(
                {"resourceType": "Bundle", "type": "transaction", "entry": []}
            )

    def test_constructor_validation(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            FhirClient(FHIR_BASE, http=httpx.AsyncClient(), max_attempts=0)


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def smart(fhir: FakeFhirServer, *, key: Any = None, **kwargs: Any) -> SmartBackendAuth:
    key = key or rsa_key()
    fhir.smart_public_key = key.public_key()
    return SmartBackendAuth(
        client_id="cwo-backend",
        private_key=key,
        key_id="cwo-2026-10",
        algorithm=kwargs.pop("algorithm", "RS384"),
        fhir_base_url=FHIR_BASE,
        http=httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler)),
        **kwargs,
    )


class TestSmart:
    async def test_discovers_and_obtains_token(self, fhir: FakeFhirServer) -> None:
        auth = smart(fhir)
        token = await auth.access_token()
        assert token in fhir.issued_tokens
        assert await auth.access_token() == token  # cached
        token_requests = [r for r in fhir.requests if str(r.url) == TOKEN_URL]
        assert len(token_requests) == 1
        form = dict(pair.split("=", 1) for pair in token_requests[0].content.decode().split("&"))
        assert form["grant_type"] == "client_credentials"
        assert "system%2FCarePlan.write" in form["scope"]

    async def test_assertion_claims(self, fhir: FakeFhirServer) -> None:
        key = rsa_key()
        auth = smart(fhir, key=key, clock=lambda: 1_800_000_000.0)
        assertion = auth.client_assertion(TOKEN_URL)
        header = jwt.get_unverified_header(assertion)
        assert header == {"alg": "RS384", "kid": "cwo-2026-10", "typ": "JWT"}
        claims = jwt.decode(
            assertion,
            key.public_key(),
            algorithms=["RS384"],
            audience=TOKEN_URL,
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == claims["sub"] == "cwo-backend"
        assert claims["exp"] - claims["iat"] == 300
        assert auth.client_assertion(TOKEN_URL) != assertion  # fresh jti every time

    async def test_es384_and_fhir_client_integration(self, fhir: FakeFhirServer) -> None:
        key = ec.generate_private_key(ec.SECP384R1())
        auth = smart(fhir, key=key, algorithm="ES384", token_url=TOKEN_URL)
        fhir.require_token = True
        client = FhirClient(
            FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler)),
            auth=auth,
            sleep=_no_sleep,
        )
        assert (await client.read("Patient", "cwo-p1"))["id"] == "cwo-p1"
        fhir.issued_tokens.clear()  # server-side revocation: next call gets 401, then refreshes
        assert (await client.read("Encounter", "cwo-e1"))["id"] == "cwo-e1"

    async def test_token_refresh_after_expiry(self, fhir: FakeFhirServer) -> None:
        now = [time.time() - 250]
        auth = smart(fhir, clock=lambda: now[0])
        first = await auth.access_token()
        now[0] += 250  # inside the refresh margin of the 300 s token
        assert await auth.access_token() != first

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (httpx.Response(401, json={"error": "invalid_client"}), "invalid_client"),
            (httpx.Response(500, text="boom"), "HTTP 500"),
            (httpx.Response(200, text="not json"), "not JSON"),
            (httpx.Response(200, json={"token_type": "bearer"}), "no access_token"),
        ],
    )
    async def test_token_endpoint_failures(self, response: httpx.Response, message: str) -> None:
        auth = SmartBackendAuth(
            client_id="c",
            private_key=rsa_key(),
            key_id="k",
            algorithm="RS384",
            fhir_base_url=FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)),
            token_url=TOKEN_URL,
        )
        with pytest.raises(SmartAuthError, match=message):
            await auth.access_token()

    async def test_discovery_failures(self) -> None:
        responses = iter([httpx.Response(404), httpx.Response(200, json={"issuer": "x"})])
        auth = SmartBackendAuth(
            client_id="c",
            private_key=rsa_key(),
            key_id="k",
            algorithm="RS384",
            fhir_base_url=FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))),
        )
        with pytest.raises(SmartAuthError, match="discovery failed"):
            await auth.token_endpoint()
        with pytest.raises(SmartAuthError, match="no token_endpoint"):
            await auth.token_endpoint()

    async def test_unreachable_token_endpoint(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("t", request=request)

        auth = SmartBackendAuth(
            client_id="c",
            private_key=rsa_key(),
            key_id="k",
            algorithm="RS384",
            fhir_base_url=FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(down)),
            token_url=TOKEN_URL,
        )
        with pytest.raises(SmartAuthError, match="unreachable"):
            await auth.access_token()

    async def test_nonpositive_expiry_defaults(self) -> None:
        auth = SmartBackendAuth(
            client_id="c",
            private_key=rsa_key(),
            key_id="k",
            algorithm="RS384",
            fhir_base_url=FHIR_BASE,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"access_token": "t", "expires_in": -5})
                )
            ),
            token_url=TOKEN_URL,
        )
        assert await auth.access_token() == "t"

    def test_key_loading(self) -> None:
        def pem(key: Any) -> bytes:
            return bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

        assert load_signing_key(pem(rsa_key()), "RS384") is not None
        assert load_signing_key(pem(ec.generate_private_key(ec.SECP384R1())), "ES384")
        with pytest.raises(ValueError, match="RSA"):
            load_signing_key(pem(ec.generate_private_key(ec.SECP384R1())), "RS384")
        with pytest.raises(ValueError, match="P-384"):
            load_signing_key(pem(ec.generate_private_key(ec.SECP256R1())), "ES384")
        # Deliberately weak key: the test proves it is rejected.
        small = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505
        with pytest.raises(ValueError, match="2048"):
            load_signing_key(pem(small), "RS384")


def test_seed_bundle_is_a_valid_transaction() -> None:
    from tests.conftest import SEED

    bundle = json.loads(SEED.read_text(encoding="utf-8"))
    assert bundle["type"] == "transaction"
    assert all(e["request"]["method"] == "PUT" for e in bundle["entry"])
