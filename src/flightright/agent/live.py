"""Run synthetic scenarios through the real graph with live free-tier models."""

from __future__ import annotations

from datetime import datetime, timedelta

from flightright.agent.graph import Deps
from flightright.agent.judging import Judge
from flightright.agent.service import ConversationService
from flightright.config import ROOT, AppConfig
from flightright.llm.client import LLMClient
from flightright.models import Booking, Scenario
from flightright.rules.engine import EntitlementEngine
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.triage.triage import Triage

SCENARIOS = ROOT / "data" / "scenarios" / "scenarios.jsonl"


def load_scenarios(split: str | None = None) -> list[Scenario]:
    rows = (
        Scenario.model_validate_json(r)
        for r in SCENARIOS.read_text(encoding="utf-8").splitlines()
        if r
    )
    return [s for s in rows if split is None or s.split == split]


def scenario_clock(s: Scenario) -> datetime:
    """When the passenger writes: during the wait, days later, or before travelling."""
    if s.situation.timing == "past":
        return s.case.scheduled_departure + timedelta(days=7)
    if s.situation.timing == "upcoming":
        return s.case.scheduled_departure - timedelta(days=s.case.notice_given_days or 3)
    return s.case.scheduled_departure + timedelta(minutes=s.case.departure_delay_minutes // 2)


def live_engine() -> EntitlementEngine:
    full = ROOT / "data" / "ourairports" / "airports.csv"
    return EntitlementEngine.from_config(
        ROOT, airports_csv=full if full.is_file() else ROOT / "tests" / "fixtures" / "airports.csv"
    )


def service_for(
    s: Scenario,
    cfg: AppConfig,
    client: LLMClient,
    judge: Judge,
    engine: EntitlementEngine,
    triage: Triage,
) -> tuple[ConversationService, str]:
    """A fresh service with this scenario's booking registered: (service, conversation id)."""
    now = scenario_clock(s)
    airline = MockAirline(cfg.authority, ActionStore(), seed=cfg.seed, clock=lambda: now)
    airline.register_booking(
        Booking(
            booking_ref=s.booking_ref,
            passenger_ref=s.passenger_ref,
            passenger_name=s.persona.name,
            loyalty_tier=s.persona.loyalty_tier,
            case=s.case,
        )
    )
    history = HistoryStore(clock=lambda: now)
    # Earlier contacts the history store knows about.
    for n in range(s.situation.repeat_contact_count):
        history.record_contact(s.passenger_ref, f"earlier-{n}", s.booking_ref, "email")
    tools = AgentTools(airline, history, Playbooks(), clock=lambda: now)
    deps = Deps(
        config=cfg,
        engine=engine,
        triage=triage,
        client=client,
        tools=tools,
        judge=judge,
        clock=lambda: now,
    )
    service = ConversationService(deps)
    return service, service.create_conversation("demo", booking_ref=s.booking_ref)
