"""Process entry point: ``python -m clinical_workflow_orchestration``."""

from __future__ import annotations

import os

import uvicorn
from agentic_core.observability import configure_logging, configure_telemetry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from clinical_workflow_orchestration import __version__
from clinical_workflow_orchestration.api.app import create_app
from clinical_workflow_orchestration.config import Settings
from clinical_workflow_orchestration.runtime.container import build_container

SERVICE = "clinical-workflow-orchestration"


def main() -> None:
    settings = Settings()  # populated from CWO_* environment variables
    configure_logging(service=SERVICE, environment=settings.environment, level=settings.log_level)
    exporter = None
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    configure_telemetry(
        service_name=SERVICE,
        service_version=__version__,
        environment=settings.environment,
        span_exporter=exporter,
    )
    app = create_app(lambda: build_container(settings))
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    uvicorn.run(
        app,
        host=os.environ.get("CWO_HOST", "127.0.0.1"),
        port=int(os.environ.get("CWO_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("CWO_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
