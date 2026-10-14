"""Clinician and staff authentication via the enterprise OIDC provider (e.g. Microsoft Entra ID).

The signing clinician's FHIR identity comes from the SMART ``fhirUser``
claim (``Practitioner/<id>``), configured as an optional claim on the IdP
app registration, so a Provenance signer always resolves to a real
Practitioner resource.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from clinical_workflow_orchestration.domain.errors import UnauthorizedError
from clinical_workflow_orchestration.fhir.resources import reference_id
from clinical_workflow_orchestration.security.jwks import KeyResolver, verify_jwt


class UserRole(StrEnum):
    CARE_COORDINATOR = "care-coordinator"  # start, resume, cancel and read workflows
    CLINICIAN_APPROVER = "clinician-approver"  # sign, reject or return care plans
    CLINICAL_AUDITOR = "clinical-auditor"  # read-only


class UserPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    roles: frozenset[str]
    fhir_user: str | None
    token_id: str | None


class UserAuthenticator:
    def __init__(
        self,
        resolver: KeyResolver,
        *,
        issuer: str,
        audience: str,
        roles_claim: str = "roles",
        fhir_user_claim: str = "fhirUser",
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._roles_claim = roles_claim
        self._fhir_user_claim = fhir_user_claim

    async def authenticate(self, authorization: str | None) -> UserPrincipal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError("bearer token required")
        claims = await verify_jwt(
            authorization[7:].strip(), self._resolver, issuer=self._issuer, audience=self._audience
        )
        raw_roles = claims.get(self._roles_claim, [])
        roles = (
            frozenset(r for r in raw_roles if isinstance(r, str))
            if isinstance(raw_roles, list)
            else frozenset()
        )
        fhir_user = claims.get(self._fhir_user_claim)
        practitioner = None
        if isinstance(fhir_user, str):
            practitioner_id = reference_id(fhir_user, "Practitioner")
            practitioner = f"Practitioner/{practitioner_id}" if practitioner_id else None
        jti = claims.get("jti")
        return UserPrincipal(
            subject=str(claims["sub"]),
            roles=roles,
            fhir_user=practitioner,
            token_id=jti if isinstance(jti, str) else None,
        )
