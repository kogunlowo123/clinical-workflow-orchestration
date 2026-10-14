"""Service metrics exposed on /metrics in Prometheus text format."""

from __future__ import annotations

from agentic_core.models.router import Attempt
from agentic_core.models.types import ModelRequest, ModelResponse
from prometheus_client import CollectorRegistry, Counter, Histogram


class OrchestrationMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "cwo_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self.transitions = Counter(
            "cwo_workflow_transitions_total",
            "Workflow state transitions",
            ["from_state", "to_state"],
            registry=self.registry,
        )
        self.step_duration = Histogram(
            "cwo_workflow_step_duration_seconds",
            "Duration of workflow steps",
            ["step", "outcome"],
            registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
        )
        self.failures = Counter(
            "cwo_workflow_failures_total",
            "Failed workflow steps by step and failure code",
            ["step", "code"],
            registry=self.registry,
        )
        self.findings = Counter(
            "cwo_validation_findings_total",
            "Validation findings by check and severity",
            ["check", "severity"],
            registry=self.registry,
        )
        self.signoffs = Counter(
            "cwo_signoffs_total",
            "Clinician decisions on drafted care plans",
            ["decision"],
            registry=self.registry,
        )
        self.policy_decisions = Counter(
            "cwo_policy_decisions_total",
            "Governance policy decisions",
            ["action", "decision"],
            registry=self.registry,
        )
        self.agent_calls = Counter(
            "cwo_inbound_agent_decisions_total",
            "Authorization decisions for inbound agent calls",
            ["channel", "action", "decision"],
            registry=self.registry,
        )
        self.fhir_requests = Counter(
            "cwo_fhir_requests_total",
            "FHIR requests by operation and outcome (HTTP status or transport_error)",
            ["operation", "outcome"],
            registry=self.registry,
        )
        self.model_attempts = Counter(
            "cwo_model_attempts_total",
            "Model provider attempts by outcome",
            ["task", "provider", "outcome"],
            registry=self.registry,
        )
        self.model_tokens = Counter(
            "cwo_model_tokens_total",
            "Model tokens consumed",
            ["task", "provider", "type"],
            registry=self.registry,
        )
        self.model_cost = Counter(
            "cwo_model_cost_usd_total",
            "Estimated model spend in USD",
            ["task", "provider"],
            registry=self.registry,
        )

    def observe_fhir(self, operation: str, outcome: str) -> None:
        self.fhir_requests.labels(operation, outcome).inc()


class PrometheusModelObserver:
    """agentic-core RouterObserver that feeds the Prometheus model metrics."""

    def __init__(self, metrics: OrchestrationMetrics) -> None:
        self._metrics = metrics

    def on_attempt(self, request: ModelRequest, attempt: Attempt) -> None:
        self._metrics.model_attempts.labels(
            request.task, attempt.provider, attempt.outcome.value
        ).inc()

    def on_response(self, request: ModelRequest, response: ModelResponse) -> None:
        tokens = self._metrics.model_tokens
        tokens.labels(request.task, response.provider, "input").inc(response.usage.input_tokens)
        tokens.labels(request.task, response.provider, "output").inc(response.usage.output_tokens)
        self._metrics.model_cost.labels(request.task, response.provider).inc(response.cost_usd)
