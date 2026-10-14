"""Domain errors mapped to HTTP status codes and OAuth-style error bodies by the API layer."""

from __future__ import annotations


class OrchestrationError(Exception):
    code = "orchestration_error"
    status = 400
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(OrchestrationError):
    code = "not_found"
    status = 404


class ConflictError(OrchestrationError):
    code = "conflict"
    status = 409


class InvalidStateError(OrchestrationError):
    code = "invalid_state"
    status = 409


class UnauthorizedError(OrchestrationError):
    code = "unauthorized"
    status = 401


class ForbiddenError(OrchestrationError):
    code = "forbidden"
    status = 403


class ApprovalRequiredError(OrchestrationError):
    """Policy requires a human decision before the action may proceed."""

    code = "approval_required"
    status = 403


class FhirError(OrchestrationError):
    """The FHIR server rejected a request or could not be reached."""

    code = "fhir_error"
    status = 502

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class FhirNotFoundError(FhirError):
    """The FHIR server has no resource with the requested id."""

    code = "fhir_not_found"
    status = 404

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404, retryable=False)


class ModelOutputError(OrchestrationError):
    """A model did not return output matching the required schema."""

    code = "model_output_invalid"
    status = 502
    retryable = True


class SmartAuthError(OrchestrationError):
    """SMART backend-services authorization against the FHIR server failed."""

    code = "smart_auth_failed"
    status = 502
    retryable = True


class IdentityFabricError(OrchestrationError):
    """The agent identity fabric could not be reached or returned an error."""

    code = "identity_fabric_unavailable"
    status = 503
    retryable = True
