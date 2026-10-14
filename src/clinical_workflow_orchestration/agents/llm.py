"""Guarded, schema-validated model calls through the agentic-core ModelRouter.

Every call:

1. is labelled ``RESTRICTED`` (PHI), so the router only considers providers
   approved for that classification;
2. passes the ContentGuard on input: clinical record content is supplied as
   EXTERNAL-trust data, spotlighted between random boundaries, scanned for
   prompt injection and secrets, and PII-redacted;
3. passes the ContentGuard on output (secrets block, PII is redacted);
4. must parse as JSON and validate against the Pydantic schema; on failure
   the model gets one corrective turn quoting the validation errors.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass

from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.domain.errors import SecurityViolationError
from agentic_core.domain.provenance import content_digest
from agentic_core.models.router import ModelRouter
from agentic_core.models.types import Message, ModelRequest, Role
from agentic_core.security import ContentGuard
from pydantic import BaseModel, ValidationError

from clinical_workflow_orchestration.domain.careplan import ModelProvenance
from clinical_workflow_orchestration.domain.errors import ModelOutputError

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    provenance: ModelProvenance


@functools.cache
def schema_json(schema: type[BaseModel]) -> str:
    """Compact JSON Schema of a response model (cached: schemas are static)."""
    return json.dumps(schema.model_json_schema(), separators=(",", ":"))


def extract_json(text: str) -> str:
    """The outermost JSON object in a model reply (tolerates code fences and preamble)."""
    stripped = _FENCE.sub("", text.strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return stripped[start : end + 1]


class StructuredModelClient:
    def __init__(
        self,
        router: ModelRouter,
        guard: ContentGuard,
        *,
        max_tokens: int = 2048,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._router = router
        self._guard = guard
        self._max_tokens = max_tokens
        self._max_attempts = max_attempts

    async def complete[T: BaseModel](
        self,
        *,
        task: str,
        system: str,
        instruction: str,
        record: str,
        schema: type[T],
    ) -> StructuredResult[T]:
        schema_text = schema_json(schema)
        messages: list[Message] = [
            Message(
                role=Role.SYSTEM,
                content=(
                    f"{system}\n\nRespond with a single JSON object that validates against this "
                    f"JSON Schema and nothing else:\n{schema_text}"
                ),
                trust=TrustLevel.SYSTEM,
            ),
            Message(role=Role.USER, content=record, trust=TrustLevel.EXTERNAL),
            Message(role=Role.USER, content=instruction, trust=TrustLevel.VERIFIED),
        ]
        request_ids: list[str] = []
        input_tokens = output_tokens = 0
        cost = 0.0
        last_error = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            request = ModelRequest(
                messages=tuple(messages),
                max_tokens=self._max_tokens,
                temperature=0.0,
                classification=DataClassification.RESTRICTED,
                task=task,
            )
            inspected = self._guard.inspect_request(request)
            if inspected.blocked or inspected.request is None:
                raise SecurityViolationError(
                    "model input blocked by content guard: " + "; ".join(inspected.reasons),
                    findings=[f"{f.category}:{f.kind}" for f in inspected.findings],
                )
            routed = await self._router.generate(inspected.request)
            response = routed.response
            request_ids.append(response.request_id)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            cost += response.cost_usd
            output = self._guard.inspect_output(response.text)
            if output.blocked or output.text is None:
                raise SecurityViolationError(
                    "model output blocked by content guard: " + "; ".join(output.reasons),
                    findings=[f"{f.category}:{f.kind}" for f in output.findings],
                )
            try:
                value = schema.model_validate_json(extract_json(output.text))
            except (ValueError, ValidationError) as exc:
                last_error = _describe(exc)
                messages.append(
                    Message(role=Role.ASSISTANT, content=output.text, trust=TrustLevel.EXTERNAL)
                )
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "That reply did not validate against the schema: "
                            f"{last_error}. Reply again with only the corrected JSON object."
                        ),
                        trust=TrustLevel.VERIFIED,
                    )
                )
                continue
            return StructuredResult(
                value=value,
                provenance=ModelProvenance(
                    provider=response.provider,
                    model=response.model,
                    request_ids=tuple(request_ids),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=round(cost, 6),
                    attempts=attempt,
                    output_digest=content_digest(output.text),
                ),
            )
        raise ModelOutputError(
            f"{task}: model output failed schema validation after {self._max_attempts} "
            f"attempts ({last_error})"
        )


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        problems = [
            f"{'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()[:5]
        ]
        return "; ".join(problems)
    return str(exc)[:300]
