"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime

import httpx
import pytest

from clinical_workflow_orchestration.clinical.evidence import EvidencePacket, EvidenceRetriever
from clinical_workflow_orchestration.clinical.rules import validate_plan
from clinical_workflow_orchestration.domain.careplan import CarePlanProposal
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.runtime.container import Container
from tests.conftest import FHIR_BASE, RISKY_DRAFT, SAFE_DRAFT, FakeFhirServer
from tests.helpers import start_and_wait

pytestmark = pytest.mark.performance

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
async def packet(fhir: FakeFhirServer) -> EvidencePacket:
    client = FhirClient(
        FHIR_BASE, http=httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler))
    )
    return await EvidenceRetriever(client, agent_id="o").for_encounter("cwo-e1", now=NOW)


async def test_deterministic_validation_budget(packet: EvidencePacket) -> None:
    plans = [
        CarePlanProposal.model_validate(copy.deepcopy(SAFE_DRAFT)),
        CarePlanProposal.model_validate(copy.deepcopy(RISKY_DRAFT)),
    ]
    started = time.perf_counter()
    for index in range(500):
        validate_plan(packet, plans[index % 2])
    per_plan_ms = (time.perf_counter() - started) * 1000 / 500
    assert per_plan_ms < 10


async def test_evidence_retrieval_budget(fhir: FakeFhirServer) -> None:
    client = FhirClient(
        FHIR_BASE, http=httpx.AsyncClient(transport=httpx.MockTransport(fhir.handler))
    )
    retriever = EvidenceRetriever(client, agent_id="o")
    started = time.perf_counter()
    for _ in range(50):
        await retriever.for_encounter("cwo-e1", now=NOW)
    per_packet_ms = (time.perf_counter() - started) * 1000 / 50
    assert per_packet_ms < 100  # orchestration overhead only; the EHR adds its own latency


async def test_orchestration_overhead_budget(container: Container) -> None:
    """Start to awaiting-approval with instant FHIR and models: the platform's own cost.

    Budget from the SLO in docs/architecture/agent-specification.md: orchestration
    overhead (persistence, audit signing, content guard, validation) under 1 s per
    workflow; EHR and model latency come on top and are measured in production.
    """
    started = time.perf_counter()
    for _ in range(10):
        await start_and_wait(container)
    per_workflow_ms = (time.perf_counter() - started) * 1000 / 10
    assert per_workflow_ms < 1000
