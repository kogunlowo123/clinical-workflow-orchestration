"""Composition root: everything the service needs, built once per process from Settings."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.models.factory import ProvidersConfig, build_providers
from agentic_core.models.router import ModelRouter
from agentic_core.observability import ModelTelemetryObserver
from agentic_core.policy import PolicyEngine, PolicySet
from agentic_core.security import ContentGuard
from sqlalchemy.ext.asyncio import AsyncEngine

from clinical_workflow_orchestration.agents.care_coordination import CareCoordinationAgent
from clinical_workflow_orchestration.agents.clinical_validation import ClinicalValidationAgent
from clinical_workflow_orchestration.agents.llm import StructuredModelClient
from clinical_workflow_orchestration.clinical.evidence import EvidenceRetriever
from clinical_workflow_orchestration.config import Settings
from clinical_workflow_orchestration.domain.ports import Clock
from clinical_workflow_orchestration.fhir.client import FhirClient
from clinical_workflow_orchestration.fhir.smart import SmartBackendAuth, load_signing_key
from clinical_workflow_orchestration.governance.approval import Governance
from clinical_workflow_orchestration.infrastructure.database import (
    SqlWorkflowRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from clinical_workflow_orchestration.integrations.agent_framework import ClinicalWorkflowAgent
from clinical_workflow_orchestration.integrations.identity_fabric import (
    IdentityFabricClient,
    ServiceCredentials,
)
from clinical_workflow_orchestration.interop.a2a import A2AHandler
from clinical_workflow_orchestration.interop.skills import SkillRouter
from clinical_workflow_orchestration.observability.metrics import (
    OrchestrationMetrics,
    PrometheusModelObserver,
)
from clinical_workflow_orchestration.orchestration.commit import CarePlanCommitter, FabricWriteGate
from clinical_workflow_orchestration.orchestration.engine import WorkflowEngine
from clinical_workflow_orchestration.orchestration.runner import WorkflowRunner
from clinical_workflow_orchestration.orchestration.service import ClinicalWorkflowService
from clinical_workflow_orchestration.security.agents import (
    AgentAuthorizer,
    FabricAgentAuthorizer,
    LocalAgentAuthorizer,
)
from clinical_workflow_orchestration.security.jwks import KeyResolver, RemoteJwks
from clinical_workflow_orchestration.security.signing import PlatformKey
from clinical_workflow_orchestration.security.users import UserAuthenticator


@dataclass
class Container:
    settings: Settings
    service: ClinicalWorkflowService
    engine_runner: WorkflowRunner
    skills: SkillRouter
    a2a: A2AHandler
    users: UserAuthenticator
    signer: PlatformKey
    metrics: OrchestrationMetrics
    audit: AuditLog
    router: ModelRouter
    engine: AsyncEngine
    http: httpx.AsyncClient
    clock: Clock
    agent_framework: ClinicalWorkflowAgent | None = None

    async def aclose(self) -> None:
        await self.engine_runner.aclose()
        await self.router.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def build_container(
    settings: Settings,
    *,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    user_resolver: KeyResolver | None = None,
    agent_resolver: KeyResolver | None = None,
) -> Container:
    """Build the object graph.

    ``http`` carries FHIR, SMART, JWKS and identity-fabric traffic;
    ``model_transport`` carries model-provider traffic. Tests inject
    ``httpx.MockTransport`` instances that replay recorded wire shapes.
    """
    clock = clock or SystemClock()
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(settings.fhir_timeout_seconds))
    metrics = OrchestrationMetrics()

    signer = (
        PlatformKey.ephemeral()
        if settings.dev_ephemeral_keys or settings.signing_key_file is None
        else PlatformKey.from_file(settings.signing_key_file)
    )
    audit = AuditLog(JsonlAuditSink(settings.audit_log_path), signer, key_id=signer.key_id)

    engine = make_engine(settings.database_url)
    await create_schema(engine)
    repository = SqlWorkflowRepository(engine)

    smart = None
    if settings.smart_client_id and settings.smart_private_key_file and settings.smart_key_id:
        smart = SmartBackendAuth(
            client_id=settings.smart_client_id,
            private_key=load_signing_key(
                settings.smart_private_key_file.read_bytes(), settings.smart_algorithm
            ),
            key_id=settings.smart_key_id,
            algorithm=settings.smart_algorithm,
            fhir_base_url=settings.fhir_base_url_str,
            http=http,
            token_url=settings.smart_token_url,
        )
    fhir = FhirClient(
        settings.fhir_base_url_str, http=http, auth=smart, observer=metrics.observe_fhir
    )

    providers = build_providers(
        ProvidersConfig.from_file(settings.providers_file, env=env),
        env=env,
        transport=model_transport,
    )
    router = ModelRouter(
        providers, observers=(ModelTelemetryObserver(), PrometheusModelObserver(metrics))
    )
    model = StructuredModelClient(router, ContentGuard(), max_tokens=settings.model_max_tokens)

    policy = PolicyEngine(PolicySet.from_file(settings.policy_file))
    governance = Governance(policy, orchestrator_id=settings.agent_id)

    fabric: IdentityFabricClient | None = None
    credentials: ServiceCredentials | None = None
    authorizer: AgentAuthorizer
    if settings.identity_fabric_url and settings.workload_token_file is not None:
        fabric = IdentityFabricClient(settings.identity_fabric_url, http=http)
        credentials = ServiceCredentials(
            fabric, agent_id=settings.agent_id, workload_token_file=settings.workload_token_file
        )
        authorizer = FabricAgentAuthorizer(fabric)
    else:
        resolver = agent_resolver
        if resolver is None and settings.agent_jwks_url:
            resolver = RemoteJwks(settings.agent_jwks_url, client=http)
        authorizer = LocalAgentAuthorizer(
            resolver,
            issuer=settings.agent_token_issuer,
            audience=settings.agent_token_audience,
            policy=policy,
        )

    retriever = EvidenceRetriever(
        fhir, agent_id=settings.agent_id, max_pages=settings.fhir_max_pages
    )
    committer = CarePlanCommitter(
        fhir,
        governance,
        signer,
        identifier_system=settings.identifier_system,
        agent_id=settings.drafting_agent,
        orchestrator_id=settings.agent_id,
        write_gate=FabricWriteGate(fabric, credentials) if fabric and credentials else None,
    )
    workflow_engine = WorkflowEngine(
        repository=repository,
        retriever=retriever,
        coordinator=CareCoordinationAgent(model, agent_id=settings.drafting_agent),
        validator=ClinicalValidationAgent(model, agent_id=settings.validation_agent),
        governance=governance,
        committer=committer,
        audit=audit,
        metrics=metrics,
        clock=clock,
        owner=_owner(),
        lease_seconds=settings.lease_seconds,
        max_auto_retries=settings.max_auto_retries,
        retry_base_seconds=settings.retry_base_seconds,
    )
    runner = WorkflowRunner(
        workflow_engine,
        repository,
        clock,
        concurrency=settings.runner_concurrency,
        sweep_interval_s=settings.sweep_interval_seconds,
    )
    service = ClinicalWorkflowService(
        repository=repository,
        engine=workflow_engine,
        governance=governance,
        retriever=retriever,
        signer=signer,
        audit=audit,
        metrics=metrics,
        clock=clock,
        max_revisions=settings.max_revisions,
    )
    service.set_scheduler(runner.submit)
    skills = SkillRouter(service, authorizer, audit=audit, metrics=metrics)
    users = UserAuthenticator(
        user_resolver or RemoteJwks(settings.user_jwks_url, client=http),
        issuer=settings.user_issuer,
        audience=settings.user_audience,
        roles_claim=settings.user_roles_claim,
        fhir_user_claim=settings.user_fhir_user_claim,
    )
    return Container(
        settings=settings,
        service=service,
        engine_runner=runner,
        skills=skills,
        a2a=A2AHandler(skills),
        users=users,
        signer=signer,
        metrics=metrics,
        audit=audit,
        router=router,
        engine=engine,
        http=http,
        clock=clock,
        agent_framework=(
            ClinicalWorkflowAgent(skills, credential=credentials.token) if credentials else None
        ),
    )
