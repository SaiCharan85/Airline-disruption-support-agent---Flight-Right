"""Multi-turn simulation (FR-13): a CrewAI passenger talks to the real graph until the issue is
settled, the case is handed to a human (resolved by the oracle), or the turn limit is reached."""

from __future__ import annotations

from typing import Literal

from crewai.llms.base_llm import BaseLLM
from pydantic import BaseModel, Field

from flightright.agent.service import ConversationService
from flightright.models import Resolution, Scenario
from flightright.scenarios.writer import describe
from flightright.services.history import PriorCase
from flightright.services.history import Resolution as HistoryResolution
from flightright.simulator import oracle
from flightright.simulator.crew import END, next_message, passenger_agent


class TurnRecord(BaseModel):
    turn: int
    passenger: str
    reply: str
    status: str
    tier: str | None = None
    attempts: int = 0
    judge_decisions: list[str] = Field(default_factory=list)


class SimOutcome(BaseModel):
    scenario_id: str
    gold_tier: str
    turns: list[TurnRecord]
    ended_by: Literal["passenger_done", "handoff", "max_turns", "simulator_unavailable"]
    handed_off: bool
    resolution: Resolution | None = None
    intents: list[str] = Field(default_factory=list)

    @property
    def n_turns(self) -> int:
        return len(self.turns)


def simulate(
    scenario: Scenario,
    service: ConversationService,
    conversation_id: str,
    llm: BaseLLM,
    *,
    max_turns: int = 4,
    oracle_noise: float = 0.0,
    seed: int = 0,
) -> SimOutcome:
    airports = service.deps.engine.airports
    agent = passenger_agent(llm, scenario.persona.name, describe(scenario, airports))
    intents = list(scenario.follow_ups)
    message = scenario.opening_message
    turns: list[TurnRecord] = []
    ended_by: str = "max_turns"
    for n in range(1, max_turns + 1):
        out = service.handle_message(conversation_id, message, f"sim-{n}")
        st = service.state(conversation_id)
        turns.append(
            TurnRecord(
                turn=n,
                passenger=message,
                reply=out.reply_text,
                status=out.status,
                tier=(st.get("triage") or {}).get("tier"),
                attempts=len(st.get("attempts") or []),
                judge_decisions=[
                    v.get("judgement", {}).get("decision", v.get("decision", ""))
                    for v in st.get("verdicts") or []
                ],
            )
        )
        if out.status == "handed_off":
            ended_by = "handoff"
            break
        if n == max_turns:
            break
        intent = intents.pop(0) if intents else None
        message = next_message(agent, st["transcript"], intent)
        if message.startswith(END):
            ended_by = "simulator_unavailable" if "unavailable" in message else "passenger_done"
            break
    resolution = None
    if ended_by == "handoff":
        st = service.state(conversation_id)
        now = service.deps.clock()
        resolution = oracle.resolve(scenario, now, noise=oracle_noise, seed=seed)
        ticket = st["ticket"]
        history = service.deps.tools.history
        history.add_case(
            scenario.passenger_ref,
            PriorCase(
                case_id=ticket["ticket_id"],
                booking_ref=scenario.booking_ref,
                disruption_type=scenario.case.disruption_type,
                summary=ticket["summary"],
                status="open",
                opened_at=now,
            ),
        )
        # FR-9.5: the human resolution becomes episodic memory for later contacts.
        history.add_resolution(
            HistoryResolution(
                case_id=ticket["ticket_id"],
                decision=resolution.decision,
                notes=resolution.notes,
                compensation_paid_eur=resolution.compensation_paid_eur,
                resolved_at=resolution.resolved_at,
            )
        )
    return SimOutcome(
        scenario_id=scenario.scenario_id,
        gold_tier=scenario.gold_tier,
        turns=turns,
        ended_by=ended_by,  # type: ignore[arg-type]
        handed_off=ended_by == "handoff",
        resolution=resolution,
        intents=list(scenario.follow_ups),
    )
