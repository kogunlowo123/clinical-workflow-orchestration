"""Configuration, composition root, published contracts, runner and the Agent Framework adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from clinical_workflow_orchestration.config import Settings
from clinical_workflow_orchestration.contracts import main as contracts_main
from clinical_workflow_orchestration.integrations.agent_framework import (
    AgentMessage,
    ClinicalWorkflowAgent,
)
from clinical_workflow_orchestration.orchestration.runner import WorkflowRunner
from clinical_workflow_orchestration.runtime.container import Container
from clinical_workflow_orchestration.security.agents import FabricAgentAuthorizer
from tests.conftest import ROOT, ContainerBuilder, FakeClock, FakeIdp, agent_headers
from tests.unit.test_identity import FakeFabric


def base_settings(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "environment": "test",
        "public_url": "https://cwo.hospital.test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "fhir_base_url": "https://fhir.hospital.test/fhir",
        "dev_ephemeral_keys": True,
        "agent_id": "spiffe://agents.test/agent/clinical/orchestrator",
        "user_issuer": "https://login.hospital.test",
        "user_audience": "api://cwo",
        "user_jwks_url": "https://login.hospital.test/jwks",
    }
    values.update(overrides)
    return values


def pem(key: Any) -> bytes:
    return bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings(**base_settings())
        assert settings.public_url_str == "https://cwo.hospital.test"
        assert settings.fhir_base_url_str == "https://fhir.hospital.test/fhir"
        assert settings.drafting_agent.endswith("/care-coordination")
        assert settings.validation_agent.endswith("/clinical-validation")

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"environment": "production"}, "dev_ephemeral_keys"),
            ({"dev_ephemeral_keys": False}, "CWO_SIGNING_KEY_FILE"),
            ({"smart_client_id": "c"}, "together"),
            ({"identity_fabric_url": "https://fabric.test"}, "CWO_WORKLOAD_TOKEN_FILE"),
            ({"agent_jwks_url": "https://x/jwks"}, "together"),
            (
                {
                    "environment": "production",
                    "dev_ephemeral_keys": False,
                    "signing_key_file": "k.pem",
                },
                "SMART",
            ),
            (
                {
                    "environment": "production",
                    "dev_ephemeral_keys": False,
                    "signing_key_file": "k.pem",
                    "smart_client_id": "c",
                    "smart_private_key_file": "s.pem",
                    "smart_key_id": "k",
                    "fhir_base_url": "http://fhir.hospital.test/fhir",
                },
                "https",
            ),
            (
                {
                    "environment": "production",
                    "dev_ephemeral_keys": False,
                    "signing_key_file": "k.pem",
                    "smart_client_id": "c",
                    "smart_private_key_file": "s.pem",
                    "smart_key_id": "k",
                },
                "identity fabric or an agent-token verifier",
            ),
        ],
    )
    def test_validation(self, overrides: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            Settings(**base_settings(**overrides))


class TestContainer:
    async def test_production_shaped_wiring(
        self, build: ContainerBuilder, settings: Settings, tmp_path: Path
    ) -> None:
        signing = tmp_path / "cwo-2026-10.pem"
        signing.write_bytes(pem(Ed25519PrivateKey.generate()))
        smart_key = tmp_path / "smart.pem"
        smart_key.write_bytes(pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
        token_file = tmp_path / "token"
        token_file.write_text("workload", encoding="utf-8")
        fake = FakeFabric()
        configured = settings.model_copy(
            update={
                "dev_ephemeral_keys": False,
                "signing_key_file": signing,
                "smart_client_id": "cwo",
                "smart_private_key_file": smart_key,
                "smart_key_id": "smart-1",
                "smart_token_url": "https://auth.hospital.test/oauth2/token",
                "identity_fabric_url": "https://identity.agents.test",
                "workload_token_file": token_file,
            }
        )
        container: Container = await build(
            settings=configured,
            http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            user_resolver=None,
            agent_resolver=None,
        )
        try:
            assert container.signer.key_id == "cwo-2026-10"
            assert isinstance(container.skills._authorizer, FabricAgentAuthorizer)  # type: ignore[attr-defined]
            assert container.agent_framework is not None
        finally:
            await container.aclose()

    async def test_local_mode_without_agent_verifier(
        self, build: ContainerBuilder, settings: Settings
    ) -> None:
        bare = settings.model_copy(
            update={
                "agent_token_issuer": None,
                "agent_token_audience": None,
                "agent_jwks_url": None,
            }
        )
        container: Container = await build(settings=bare, agent_resolver=None)
        try:
            assert container.agent_framework is None
        finally:
            await container.aclose()


def test_contracts_are_current() -> None:
    assert contracts_main(["--check"], root=ROOT) == 0


def test_contract_export_round_trip(tmp_path: Path) -> None:
    assert contracts_main(["--check"], root=tmp_path) == 1
    assert contracts_main([], root=tmp_path) == 0
    assert contracts_main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "schemas" / "care-plan-proposal.model-output.schema.json").is_file()
    openapi = json.loads((tmp_path / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    assert "/v1/workflows/{workflow_id}/decision" in openapi["paths"]


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = asyncio.Event()

    async def advance(self, workflow_id: str) -> None:
        self.calls.append(workflow_id)
        if workflow_id == "boom":
            raise RuntimeError("engine bug")
        await self.release.wait()


class FakeRepo:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.fail = False

    async def runnable(self, *, now: Any, limit: int = 50) -> list[str]:
        if self.fail:
            raise OSError("database down")
        return self.ids


class TestRunner:
    async def test_dedupes_reruns_and_survives_errors(self) -> None:
        engine = FakeEngine()
        repo = FakeRepo(["a"])
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await asyncio.sleep(0)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError

        runner = WorkflowRunner(engine, repo, FakeClock(), sleep=sleep)  # type: ignore[arg-type]
        runner.submit("a")
        await asyncio.sleep(0)  # let it start advancing
        runner.submit("a")  # already running: scheduled as a rerun
        runner.submit("boom")
        assert runner.active == 2
        engine.release.set()
        await runner.drain()
        assert engine.calls.count("a") == 2
        assert "boom" in engine.calls
        repo.fail = True
        runner.start()
        runner.start()  # idempotent
        await runner.aclose(grace_s=0.1)
        runner.submit("late")  # ignored after close
        assert runner.active == 0

    async def test_close_cancels_stuck_work(self) -> None:
        engine = FakeEngine()
        runner = WorkflowRunner(engine, FakeRepo([]), clock=None)  # type: ignore[arg-type]
        runner.submit("stuck")
        await asyncio.sleep(0)
        await runner.aclose(grace_s=0.01)
        assert runner.active == 0


class TestAgentFramework:
    async def test_runs_skills_with_host_credential(
        self, container: Container, agent_idp: FakeIdp
    ) -> None:
        async def credential() -> str:
            return agent_headers(agent_idp)["Authorization"][7:]

        agent = ClinicalWorkflowAgent(container.skills, credential=credential)
        thread = agent.get_new_thread()
        response = await agent.run(
            json.dumps(
                {
                    "skill": "start-care-workflow",
                    "encounter_id": "cwo-e1",
                    "purpose_of_use": "TREAT",
                }
            ),
            thread=thread,
        )
        assert response.value is not None
        assert response.value["state"] == "pending"
        assert len(thread.messages) == 2
        await container.engine_runner.drain()

        listing = await agent.run(
            [
                AgentMessage(
                    role="user",
                    text=json.dumps({"skill": "list-pending-approvals", "purpose_of_use": "TREAT"}),
                )
            ]
        )
        assert listing.value is not None
        assert len(listing.value["items"]) == 1

        updates = [u async for u in agent.run_stream({"text": json.dumps({"skill": "nope"})})]
        assert "ValueError" in updates[0].text

        denied = await agent.run(
            json.dumps(
                {"skill": "get-care-workflow", "workflow_id": "0" * 32, "purpose_of_use": "TREAT"}
            )
        )
        assert denied.value is None
        assert "NotFoundError" in denied.text
        with pytest.raises(ValueError, match="user message"):
            await agent.run(None)
        with pytest.raises(ValueError, match="user message"):
            await agent.run([])
        not_json = await agent.run("hello")
        assert "error" in json.loads(not_json.text)
