"""CrewAI passenger agents (FR-13.1), running on the project's own LLM client.

CrewAI orchestrates the role-play (persona as role/backstory, one task per turn); every model call
goes through `LLMClient`, so it gets the free-tier fallback chain, the usage ledger and the
exact-match cache (seeded, reproducible reruns: FR-13.3). CrewAI telemetry and tracing are off.
"""

from __future__ import annotations

import os

for _flag in (
    "OTEL_SDK_DISABLED",
    "CREWAI_DISABLE_TELEMETRY",
    "CREWAI_DISABLE_TRACKING",
    "CREWAI_DISABLE_VERSION_CHECK",
):
    os.environ.setdefault(_flag, "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

from typing import Any  # noqa: E402

from crewai import Agent, Crew, Process, Task  # noqa: E402
from crewai.llms.base_llm import BaseLLM  # noqa: E402
from pydantic import PrivateAttr  # noqa: E402

from flightright.llm.client import LLMClient  # noqa: E402
from flightright.llm.types import LLMUnavailableError  # noqa: E402

ROLE = "simulator"
END = "[END]"


class ClientCrewLLM(BaseLLM):
    """A CrewAI LLM backed by `LLMClient` (role `simulator`)."""

    _client: LLMClient = PrivateAttr()
    _role: str = PrivateAttr(default=ROLE)

    def __init__(self, client: LLMClient, role: str = ROLE, **data: Any) -> None:
        super().__init__(model=f"flightright:{role}", **data)
        self._client = client
        self._role = role

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> str:
        if isinstance(messages, str):
            system, user = "", messages
        else:
            system = "\n\n".join(
                str(m.get("content", "")) for m in messages if m.get("role") == "system"
            )
            user = "\n\n".join(
                str(m.get("content", "")) for m in messages if m.get("role") != "system"
            )
        try:
            return self._client.text(
                self._role, system or "You are a helpful role-player.", user
            ).value
        except LLMUnavailableError as exc:  # a dead simulator ends the conversation, never a crash
            return f"{END} (simulator unavailable: {exc})"

    def supports_function_calling(self) -> bool:
        return False


def passenger_agent(llm: BaseLLM, name: str, backstory: str) -> Agent:
    return Agent(
        role=f"Airline passenger {name}",
        goal="Get your travel problem sorted out, behaving exactly like the person described.",
        backstory=backstory,
        llm=llm,
        allow_delegation=False,
        verbose=False,
        max_iter=1,
    )


def next_message(agent: Agent, transcript: list[dict[str, str]], intent: str | None) -> str:
    """One passenger turn: CrewAI runs a single task for the persona agent."""
    convo = "\n".join(
        f"{'YOU' if t['role'] == 'passenger' else 'SUPPORT'}: {t['text']}" for t in transcript[-8:]
    )
    goal = (
        f"What you want now: {intent.replace('_', ' ')}."
        if intent
        else f"If your problem is sorted or you have nothing to add, reply exactly {END}."
    )
    task = Task(
        description=(
            "You are chatting with the airline's support team. Conversation so far:\n"
            f"{convo}\n\n{goal}\n"
            "Write your next message as this passenger, in your own voice and English level. Stay "
            "consistent with your situation; do not invent new problems; 1-4 sentences. If support has "
            f"handed you to a human colleague and you have nothing urgent to add, reply exactly {END}."
        ),
        expected_output=f"The passenger's next message only, or {END}",
        agent=agent,
    )
    out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
    return str(getattr(out, "raw", out)).strip()
