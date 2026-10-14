"""Microsoft Agent Framework adapter.

Lets a Microsoft Agent Framework host (for example a workflow built with
``agent_framework.WorkflowBuilder`` or a ``ChatAgent`` that delegates to
sub-agents) use the clinical orchestrator as one of its agents. The adapter
implements the framework's agent protocol shape (``name``, ``description``,
``get_new_thread``, ``run``, ``run_stream``) structurally, so it plugs in
without this service importing the framework or pinning its release cadence
(see ADR-0005).

Input is the last user message; its text must be a JSON object naming the
skill and its arguments, exactly as in an A2A data part. The adapter calls
the skill with the host's own agent credential, so the call is authorized
and audited like any other agent call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentic_core.domain.identifiers import new_id
from pydantic import ValidationError

from clinical_workflow_orchestration.domain.errors import OrchestrationError
from clinical_workflow_orchestration.interop.skills import Skill, SkillRouter


class TextMessage(Protocol):
    """Structural view of ``agent_framework.ChatMessage``."""

    @property
    def text(self) -> str: ...


@dataclass(frozen=True)
class AgentMessage:
    role: str
    text: str


@dataclass
class AgentThread:
    """Conversation state the framework passes back on later turns."""

    thread_id: str = field(default_factory=new_id)
    messages: list[AgentMessage] = field(default_factory=list)


@dataclass(frozen=True)
class AgentRunResponse:
    messages: tuple[AgentMessage, ...]
    response_id: str
    value: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.messages)


@dataclass(frozen=True)
class AgentRunResponseUpdate:
    text: str
    role: str = "assistant"
    response_id: str = ""


MessagesInput = (
    str | TextMessage | Mapping[str, Any] | Sequence[str | TextMessage | Mapping[str, Any]]
)
CredentialProvider = Callable[[], Awaitable[str]]


def _text(item: str | TextMessage | Mapping[str, Any]) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return str(item.get("text", ""))
    return str(item.text)


class ClinicalWorkflowAgent:
    """The orchestrator as a Microsoft Agent Framework agent."""

    def __init__(
        self,
        skills: SkillRouter,
        *,
        credential: CredentialProvider,
        name: str = "clinical-workflow-orchestrator",
        description: str = (
            "Starts and tracks clinician-signed care-coordination workflows on FHIR and checks "
            "medication safety."
        ),
    ) -> None:
        self._skills = skills
        self._credential = credential
        self.id = new_id()
        self.name = name
        self.display_name = name
        self.description = description

    def get_new_thread(self) -> AgentThread:
        return AgentThread()

    async def run(
        self,
        messages: MessagesInput | None = None,
        *,
        thread: AgentThread | None = None,
        **_: Any,
    ) -> AgentRunResponse:
        if messages is None:
            raise ValueError("a user message naming a skill is required")
        items = (
            [messages]
            if isinstance(messages, str | Mapping) or hasattr(messages, "text")
            else list(messages)
        )
        if not items:
            raise ValueError("a user message naming a skill is required")
        prompt = _text(items[-1])
        response_id = new_id()
        try:
            payload = json.loads(prompt)
            if not isinstance(payload, dict) or "skill" not in payload:
                raise ValueError("message must be a JSON object with a 'skill' field")
            skill = Skill(str(payload["skill"]))
            arguments = {k: v for k, v in payload.items() if k != "skill"}
            output = await self._skills.invoke(
                skill,
                arguments,
                authorization=f"Bearer {await self._credential()}",
                channel="agent-framework",
            )
            value: dict[str, Any] | None = output.model_dump(mode="json")
            reply = json.dumps(value, sort_keys=True)
        except (ValueError, ValidationError, OrchestrationError) as exc:
            value = None
            reply = json.dumps({"error": type(exc).__name__, "detail": str(exc)[:500]})
        answer = AgentMessage(role="assistant", text=reply)
        if thread is not None:
            thread.messages.extend([AgentMessage(role="user", text=prompt), answer])
        return AgentRunResponse(messages=(answer,), response_id=response_id, value=value)

    async def run_stream(
        self,
        messages: MessagesInput | None = None,
        *,
        thread: AgentThread | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentRunResponseUpdate]:
        response = await self.run(messages, thread=thread, **kwargs)
        for message in response.messages:
            yield AgentRunResponseUpdate(
                text=message.text, role=message.role, response_id=response.response_id
            )
