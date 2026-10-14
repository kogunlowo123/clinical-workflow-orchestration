"""Runtime settings, read from CWO_* environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CWO_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="Public base URL; used in the A2A Agent Card")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    # ------------------------------------------------------------------- FHIR
    fhir_base_url: HttpUrl = Field(description="FHIR R4 base URL of the EHR")
    smart_client_id: str | None = Field(
        default=None, description="SMART backend-services client id registered with the EHR"
    )
    smart_private_key_file: Path | None = None
    smart_key_id: str | None = None
    smart_algorithm: Literal["RS384", "ES384"] = "RS384"
    smart_token_url: str | None = Field(
        default=None, description="Token endpoint; discovered from smart-configuration if unset"
    )
    identifier_system: str = Field(
        default="urn:ietf:rfc:3986",
        description="Identifier system for CarePlan/Task identifiers written by this service",
    )
    fhir_max_pages: int = Field(default=20, ge=1, le=200)
    fhir_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # ------------------------------------------------------------- models
    providers_file: Path = Path("configs/providers.yaml")
    model_max_tokens: int = Field(default=2048, ge=256, le=16384)

    # ------------------------------------------------------------- governance
    policy_file: Path = Path("policies/clinical-workflow.yaml")
    signing_key_file: Path | None = Field(
        default=None, description="Ed25519 PEM signing sign-off attestations and the audit chain"
    )
    dev_ephemeral_keys: bool = Field(
        default=False, description="Generate an in-memory signing key. Refused in production."
    )
    audit_log_path: Path = Path("/var/lib/clinical-workflow/audit/audit.jsonl")

    # ------------------------------------------------------------- identities
    agent_id: str = Field(
        description="SPIFFE id of this orchestrator, e.g. spiffe://<domain>/agent/clinical/cwo"
    )
    drafting_agent_id: str | None = Field(
        default=None, description="SPIFFE id recorded for the care-coordination agent"
    )
    validation_agent_id: str | None = None

    user_issuer: str = Field(description="Enterprise OIDC issuer for clinicians and staff")
    user_audience: str = Field(description="Audience clinicians' access tokens carry")
    user_jwks_url: str
    user_roles_claim: str = "roles"
    user_fhir_user_claim: str = "fhirUser"

    identity_fabric_url: str | None = Field(
        default=None, description="Agent identity fabric base URL; enables fabric authorization"
    )
    workload_token_file: Path | None = Field(
        default=None, description="Projected service-account token used for jwt-bearer"
    )
    agent_token_issuer: str | None = Field(
        default=None, description="Local mode: issuer of inbound agent credentials"
    )
    agent_token_audience: str | None = None
    agent_jwks_url: str | None = None

    # ------------------------------------------------------------- execution
    max_revisions: int = Field(default=3, ge=0, le=10)
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    sweep_interval_seconds: float = Field(default=30.0, ge=1, le=600)
    max_auto_retries: int = Field(default=3, ge=0, le=10)
    retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    runner_concurrency: int = Field(default=8, ge=1, le=128)

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Settings:
        production = self.environment == "production"
        if self.dev_ephemeral_keys and production:
            raise ValueError("dev_ephemeral_keys cannot be used in production")
        if not self.dev_ephemeral_keys and self.signing_key_file is None:
            raise ValueError("CWO_SIGNING_KEY_FILE is required unless dev_ephemeral_keys is set")
        smart = (self.smart_client_id, self.smart_private_key_file, self.smart_key_id)
        if any(v is not None for v in smart) and not all(v is not None for v in smart):
            raise ValueError(
                "SMART backend services needs CWO_SMART_CLIENT_ID, CWO_SMART_PRIVATE_KEY_FILE "
                "and CWO_SMART_KEY_ID together"
            )
        if production and self.smart_client_id is None:
            raise ValueError("production requires SMART backend-services authorization")
        if production and self.fhir_base_url.scheme != "https":
            raise ValueError("production requires an https FHIR base URL")
        if self.identity_fabric_url and self.workload_token_file is None:
            raise ValueError("CWO_WORKLOAD_TOKEN_FILE is required with CWO_IDENTITY_FABRIC_URL")
        local = (self.agent_token_issuer, self.agent_token_audience, self.agent_jwks_url)
        if any(v is not None for v in local) and not all(v is not None for v in local):
            raise ValueError(
                "local agent authorization needs CWO_AGENT_TOKEN_ISSUER, "
                "CWO_AGENT_TOKEN_AUDIENCE and CWO_AGENT_JWKS_URL together"
            )
        if production and not self.identity_fabric_url and self.agent_jwks_url is None:
            raise ValueError("production requires the identity fabric or an agent-token verifier")
        return self

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")

    @property
    def fhir_base_url_str(self) -> str:
        return str(self.fhir_base_url).rstrip("/")

    @property
    def drafting_agent(self) -> str:
        return self.drafting_agent_id or f"{self.agent_id}/care-coordination"

    @property
    def validation_agent(self) -> str:
        return self.validation_agent_id or f"{self.agent_id}/clinical-validation"
