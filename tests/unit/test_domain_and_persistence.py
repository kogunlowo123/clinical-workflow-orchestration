"""Workflow state machine, SQL repository, signing key and the commit step."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from agentic_core.domain.classification import Decision
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from clinical_workflow_orchestration.domain.errors import (
    ApprovalRequiredError,
    ConflictError,
    FhirError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
)
from clinical_workflow_orchestration.domain.workflow import (
    TRANSITIONS,
    Failure,
    SignOffDecision,
    Workflow,
    WorkflowState,
)
from clinical_workflow_orchestration.fhir.resources import VERIFICATION_SIGNATURE
from clinical_workflow_orchestration.infrastructure.database import (
    SqlWorkflowRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from clinical_workflow_orchestration.orchestration.commit import CarePlanCommitter
from clinical_workflow_orchestration.runtime.container import Container
from clinical_workflow_orchestration.security.signing import PlatformKey
from tests.conftest import FakeFhirServer
from tests.helpers import CLINICIAN, approve, run_until_idle, start_and_wait

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def make_workflow(key: str = "key-1", state: WorkflowState = WorkflowState.PENDING) -> Workflow:
    return Workflow(
        id=key.replace("-", "").ljust(32, "0")[:32],
        idempotency_key=key,
        encounter_id="cwo-e1",
        state=state,
        created_by="tester",
        created_at=T0,
        updated_at=T0,
    )


class TestStateMachine:
    def test_valid_and_invalid_transitions(self) -> None:
        workflow = make_workflow()
        moved = workflow.transition(
            WorkflowState.RETRIEVING_EVIDENCE, at=T0, actor="engine", reason="started"
        )
        assert moved.state is WorkflowState.RETRIEVING_EVIDENCE
        assert moved.history[-1].from_state is WorkflowState.PENDING
        with pytest.raises(InvalidStateError):
            moved.transition(WorkflowState.COMPLETED, at=T0, actor="x", reason="skip")
        for terminal in (WorkflowState.COMPLETED, WorkflowState.REJECTED, WorkflowState.CANCELLED):
            assert TRANSITIONS[terminal] == frozenset()
        assert WorkflowState.CANCELLED not in TRANSITIONS[WorkflowState.COMMITTING]

    def test_failure_retained_while_resuming(self) -> None:
        failure = Failure(
            step=WorkflowState.DRAFTING,
            code="provider_unavailable",
            message="down",
            retryable=True,
            attempts=1,
            failed_at=T0,
        )
        failed = make_workflow(state=WorkflowState.DRAFTING).transition(
            WorkflowState.FAILED, at=T0, actor="engine", reason="x", failure=failure
        )
        resumed = failed.transition(WorkflowState.DRAFTING, at=T0, actor="engine", reason="retry")
        assert resumed.failure == failure
        done = resumed.transition(WorkflowState.VALIDATING, at=T0, actor="engine", reason="ok")
        assert done.failure is None
        assert not done.terminal


class TestRepository:
    async def test_round_trip_and_concurrency(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        await create_schema(engine)
        await create_schema(engine)  # idempotent
        repo = SqlWorkflowRepository(engine)
        workflow = await repo.add(make_workflow())
        assert (
            await repo.add(make_workflow().model_copy(update={"id": "f" * 32}))
        ).id == workflow.id
        saved = await repo.save(
            workflow.transition(WorkflowState.RETRIEVING_EVIDENCE, at=T0, actor="e", reason="r")
        )
        assert saved.version == 1
        with pytest.raises(ConflictError, match="modified concurrently"):
            await repo.save(workflow)
        with pytest.raises(NotFoundError):
            await repo.save(make_workflow("other-key"))
        assert await repo.get("missing") is None
        listed = await repo.list(state=WorkflowState.RETRIEVING_EVIDENCE)
        assert [w.id for w in listed] == [workflow.id]
        await engine.dispose()

    async def test_leases_and_runnable(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
        await create_schema(engine)
        repo = SqlWorkflowRepository(engine)
        workflow = await repo.add(make_workflow())
        assert await repo.runnable(now=T0) == [workflow.id]
        later = T0 + timedelta(minutes=5)
        assert await repo.acquire_lease(workflow.id, owner="a", now=T0, until=later)
        assert await repo.acquire_lease(workflow.id, owner="a", now=T0, until=later)  # renew
        assert not await repo.acquire_lease(workflow.id, owner="b", now=T0, until=later)
        assert await repo.runnable(now=T0) == []
        # An expired lease is taken over by another replica.
        assert await repo.acquire_lease(
            workflow.id,
            owner="b",
            now=later + timedelta(seconds=1),
            until=later + timedelta(minutes=5),
        )
        await repo.release_lease(workflow.id, owner="a")  # not the owner: no effect
        assert await repo.runnable(now=T0) == []
        await repo.release_lease(workflow.id, owner="b")
        assert await repo.runnable(now=T0) == [workflow.id]

        failed = workflow.transition(
            WorkflowState.FAILED,
            at=T0,
            actor="e",
            reason="r",
            failure=Failure(
                step=WorkflowState.PENDING,
                code="x",
                message="m",
                retryable=True,
                attempts=1,
                failed_at=T0,
                next_retry_at=T0 + timedelta(seconds=30),
            ),
        )
        await repo.save(failed)
        assert await repo.runnable(now=T0) == []
        assert await repo.runnable(now=T0 + timedelta(seconds=31)) == [workflow.id]
        await engine.dispose()

    def test_system_clock_is_utc(self) -> None:
        assert SystemClock().now().tzinfo is UTC


class TestPlatformKey:
    def test_sign_verify_and_jwks(self, tmp_path: Path) -> None:
        key = Ed25519PrivateKey.generate()
        path = tmp_path / "cwo-2026-10.pem"
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        platform = PlatformKey.from_file(path)
        assert platform.key_id == "cwo-2026-10"
        token = platform.sign_jws({"wf": "1", "iat": 1})
        assert platform.verify_jws(token)["wf"] == "1"
        jwk = platform.jwks()["keys"][0]
        public = jwt.PyJWK(jwk).key
        assert jwt.decode(token, public, algorithms=["EdDSA"], options={"verify_iat": False})
        assert len(platform.sign(b"digest")) == 64
        assert platform.public_key is not None

    def test_rejects_non_ed25519(self, tmp_path: Path) -> None:
        path = tmp_path / "ec.pem"
        path.write_bytes(
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        with pytest.raises(ValueError, match="Ed25519"):
            PlatformKey.from_file(path)


class TestCommit:
    async def test_transaction_contents(self, container: Container, fhir: FakeFhirServer) -> None:
        done = await approve(container, await start_and_wait(container))
        assert done.commit is not None
        bundle = fhir.transactions[0]
        types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        assert types == ["CarePlan", *["Task"] * 6, "Provenance"]
        careplan = bundle["entry"][0]
        assert careplan["request"]["ifNoneExist"].endswith(f"{done.id}-careplan")
        assert careplan["resource"]["author"] == {"reference": "Practitioner/cwo-pr1"}
        assert {"reference": "Condition/cwo-c1"} in careplan["resource"]["addresses"]
        lab_task = bundle["entry"][2]["resource"]
        assert lab_task["input"][0]["valueCodeableConcept"]["coding"][0]["code"] == "2823-3"
        assert lab_task["reasonReference"] == {"reference": "Observation/cwo-o1"}
        assert lab_task["restriction"]["period"]["end"] == "2026-10-16"
        med_task = bundle["entry"][5]["resource"]
        assert med_task["focus"] == {"reference": "MedicationRequest/cwo-m1"}

        stored_plan = fhir.store["CarePlan"][done.commit.care_plan.split("/")[1]]
        assert all(a["reference"]["reference"].startswith("Task/") for a in stored_plan["activity"])
        provenance = fhir.store["Provenance"][done.commit.provenance.split("/")[1]]
        assert provenance["target"][0]["reference"] == done.commit.care_plan
        signature = provenance["signature"][0]
        assert signature["type"][0]["code"] == VERIFICATION_SIGNATURE
        assert signature["who"] == {"reference": "Practitioner/cwo-pr1"}
        jws = base64.b64decode(signature["data"]).decode()
        claims = container.signer.verify_jws(jws)
        assert claims["wf"] == done.id
        assert claims["practitioner"] == "Practitioner/cwo-pr1"
        assert claims["draft_digest"] == done.draft.digest  # type: ignore[union-attr]
        assert any(
            e["what"]["reference"].startswith("Encounter/cwo-e1") for e in provenance["entity"]
        )
        roles = {a["type"]["coding"][0]["code"] for a in provenance["agent"]}
        assert roles == {"author", "verifier", "assembler"}

    async def test_commit_recovers_after_crash(
        self, container: Container, fhir: FakeFhirServer
    ) -> None:
        done = await approve(container, await start_and_wait(container))
        committer: CarePlanCommitter = container.service._engine._committer  # type: ignore[attr-defined]
        replay = await committer.commit(done, at=T0)
        assert replay.care_plan == done.commit.care_plan  # type: ignore[union-attr]
        assert replay.bundle_digest == "recovered"
        assert len(fhir.transactions) == 1  # nothing written twice

    async def test_commit_rechecks_preconditions(
        self, container: Container, fhir: FakeFhirServer
    ) -> None:
        waiting = await start_and_wait(container)
        committer: CarePlanCommitter = container.service._engine._committer  # type: ignore[attr-defined]
        with pytest.raises(InvalidStateError, match="approving clinician sign-off"):
            await committer.commit(waiting, at=T0)
        decided = await container.service.decide(
            waiting.id,
            principal=CLINICIAN,
            decision=SignOffDecision.APPROVE,
            draft_digest=waiting.draft.digest,  # type: ignore[union-attr]
            comment=None,
            acknowledgements=(),
        )
        await run_until_idle(container)
        signoff = decided.signoff
        assert signoff is not None
        forged = decided.model_copy(
            update={"signoff": signoff.model_copy(update={"practitioner": "Practitioner/evil"})}
        )
        with pytest.raises(InvalidStateError, match="does not match"):
            await committer.commit(forged, at=T0)
        other_key = PlatformKey.ephemeral().sign_jws({"wf": decided.id, "iat": 1})
        with pytest.raises(jwt.InvalidSignatureError):
            await committer.commit(
                decided.model_copy(
                    update={"signoff": signoff.model_copy(update={"signature": other_key})}
                ),
                at=T0,
            )
        unsigned = decided.model_copy(
            update={"signoff": signoff.model_copy(update={"signature": None})}
        )
        with pytest.raises(InvalidStateError, match="not signed"):
            await committer.commit(unsigned, at=T0)
        no_draft = decided.model_copy(update={"draft": None})
        with pytest.raises(InvalidStateError, match="draft and its evidence"):
            await committer.commit(no_draft, at=T0)

    async def test_policy_gate_and_partial_state(
        self, container: Container, fhir: FakeFhirServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        done = await approve(container, await start_and_wait(container))
        committer: CarePlanCommitter = container.service._engine._committer  # type: ignore[attr-defined]
        governance = committer._governance  # type: ignore[attr-defined]
        original = governance.ehr_write

        def decide_as(decision: str):  # type: ignore[no-untyped-def]
            def patched(workflow, *, at, acknowledgements=()):  # type: ignore[no-untyped-def]
                record = original(workflow, at=at)
                return record.model_copy(update={"decision": Decision(decision)})

            return patched

        monkeypatch.setattr(governance, "ehr_write", decide_as("deny"))
        with pytest.raises(ForbiddenError):
            await committer.commit(done, at=T0)
        monkeypatch.setattr(governance, "ehr_write", decide_as("require_approval"))
        with pytest.raises(ApprovalRequiredError):
            await committer.commit(done, at=T0)
        monkeypatch.setattr(governance, "ehr_write", original)
        fhir.store["Provenance"].clear()
        with pytest.raises(FhirError, match="manual reconciliation"):
            await committer.commit(done, at=T0)

    async def test_transaction_response_validation(
        self, container: Container, fhir: FakeFhirServer
    ) -> None:
        waiting = await start_and_wait(container)
        original = fhir._transaction

        def short_response(bundle):  # type: ignore[no-untyped-def]
            response = original(bundle)
            body = json.loads(response.content)
            body["entry"] = body["entry"][:2]
            return httpx.Response(200, json=body)

        fhir._transaction = short_response  # type: ignore[method-assign]
        failed = await approve(container, waiting)
        assert failed.state is WorkflowState.FAILED
        assert failed.failure is not None
        assert "expected 8" in failed.failure.message
