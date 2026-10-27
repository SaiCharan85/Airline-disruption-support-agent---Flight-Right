"""Test harness for the conversation graph: scripted LLM, mock airline, fixed clock (no network)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flightright.agent.graph import Deps
from flightright.agent.judging import CodeChecksOnlyJudge, Judge, JudgeVerdict
from flightright.agent.service import ConversationService
from flightright.config import RoleConfig, load_config
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.types import LLMError
from flightright.llm.usage import UsageLedger
from flightright.models import Booking, DisruptionCase
from flightright.rules.engine import EntitlementEngine
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.triage.triage import Triage

NOW = datetime(2027, 11, 15, 12, 0, tzinfo=UTC)
GOOD_DRAFT = (
    "I'm sorry your flight is delayed. I've issued a meal voucher of EUR 20 and a "
    "refreshments voucher of EUR 10 for while you wait. Because you will arrive more than "
    "three hours late, you are entitled to compensation of EUR 400, which we will pay by "
    "bank transfer. A colleague will confirm the payment."
)
SUMMARY = (
    '{"summary": "Passenger needs help", "passenger_state": "waiting", '
    '"recommended_next_step": "Call them"}'
)


def case(**kw: Any) -> DisruptionCase:
    base: dict[str, Any] = {
        "flight_no": "LH1166",
        "carrier_iata": "LH",
        "carrier_is_eu": True,
        "origin_iata": "FRA",
        "final_destination_iata": "LIS",
        "scheduled_departure": "2027-11-15T10:00:00+01:00",
        "booking_date": "2027-10-30",
        "disruption_type": "delay",
        "cause_code": "technical_fault",
        "departure_delay_minutes": 200,
        "arrival_delay_minutes": 200,
    }
    return DisruptionCase.model_validate(base | kw)


@dataclass
class Harness:
    service: ConversationService
    airline: MockAirline
    provider: ScriptedProvider

    def start(self, booking_ref: str | None = "ABC123") -> str:
        return self.service.create_conversation("webhook", booking_ref=booking_ref)


class ScriptedJudge:
    """Returns the scripted verdicts for replies; approves holding messages and summaries."""

    def __init__(self, verdicts: list[JudgeVerdict]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[str] = []

    def evaluate(self, **kw: Any) -> JudgeVerdict:
        self.calls.append(kw["target"])
        if kw["target"] != "reply" or not self.verdicts:
            return JudgeVerdict(decision="approve")
        return self.verdicts.pop(0)


def build_harness(
    engine: EntitlementEngine,
    script: list[str | LLMError],
    *,
    booking_case: DisruptionCase | None = None,
    judge: Judge | None = None,
    fare: int = 250,
    checkpoint: str = ":memory:",
    airline: MockAirline | None = None,
) -> Harness:
    cfg = load_config()
    mcfg = models_cfg(
        roles={
            "agent": RoleConfig(chain=["a:m1"], max_tokens=50),
            "summary": RoleConfig(chain=["a:m1"], max_tokens=50),
        }
    )
    provider = ScriptedProvider("a", list(script))
    client = LLMClient(
        mcfg, {"a": provider}, LLMCache(":memory:"), UsageLedger(":memory:", mcfg.catalog, 1.0, 0)
    )
    airline = airline or MockAirline(cfg.authority, ActionStore(), clock=lambda: NOW)
    airline.register_booking(
        Booking(
            booking_ref="ABC123",
            passenger_ref="P1",
            passenger_name="Anna Schmidt",
            fare_eur=fare,
            case=booking_case or case(),
        )
    )
    tools = AgentTools(airline, HistoryStore(clock=lambda: NOW), Playbooks(), clock=lambda: NOW)
    deps = Deps(
        config=cfg,
        engine=engine,
        triage=Triage(None),
        client=client,
        tools=tools,
        judge=judge or CodeChecksOnlyJudge(),
        clock=lambda: NOW,
    )
    return Harness(ConversationService(deps, checkpoint), airline, provider)
