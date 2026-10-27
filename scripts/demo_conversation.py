"""Play one synthetic scenario through the full conversation graph with live free-tier models.

  uv run python scripts/demo_conversation.py SC-0001           # a dev scenario's opening message
  uv run python scripts/demo_conversation.py SC-0001 --follow "Can I get a refund instead?"

Prints the passenger message, the final reply and the pipeline trace (triage, entitlement, checks).
Uses the placeholder judge until the real judge lands (Step 7b).
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from flightright.agent.graph import Deps
from flightright.agent.judging import CodeChecksOnlyJudge
from flightright.agent.service import ConversationService
from flightright.config import ROOT, load_config, load_env
from flightright.llm.client import LLMClient
from flightright.models import Booking, Scenario
from flightright.rules.engine import EntitlementEngine
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.triage.triage import Triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario_id")
    parser.add_argument(
        "--follow", action="append", default=[], help="follow-up passenger message(s)"
    )
    args = parser.parse_args()

    rows = (
        (ROOT / "data" / "scenarios" / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
    )
    scenarios = {s.scenario_id: s for s in (Scenario.model_validate_json(r) for r in rows if r)}
    s = scenarios.get(args.scenario_id)
    if s is None:
        print(f"unknown scenario {args.scenario_id}", file=sys.stderr)
        return 2
    if s.split == "test":
        print("refusing a test-split scenario (held out)", file=sys.stderr)
        return 2

    cfg = load_config()
    load_env()
    client = LLMClient.from_config(cfg.models)
    offset = {"past": timedelta(days=7), "upcoming": -timedelta(days=s.case.notice_given_days or 3)}
    now = s.case.scheduled_departure + offset.get(
        s.situation.timing, timedelta(minutes=s.case.departure_delay_minutes // 2)
    )
    engine = EntitlementEngine.from_config(
        ROOT, airports_csv=ROOT / "data" / "ourairports" / "airports.csv"
    )
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
    tools = AgentTools(airline, HistoryStore(clock=lambda: now), Playbooks(), clock=lambda: now)
    deps = Deps(
        config=cfg,
        engine=engine,
        triage=Triage(client),
        client=client,
        tools=tools,
        judge=CodeChecksOnlyJudge(),
        clock=lambda: now,
    )
    service = ConversationService(deps)
    cid = service.create_conversation("demo", booking_ref=s.booking_ref)

    for n, text in enumerate([s.opening_message, *args.follow], 1):
        out = service.handle_message(cid, text, f"demo-{n}")
        st = service.state(cid)
        print(f"\n=== PASSENGER ({s.persona.name}, gold tier {s.gold_tier})\n{text}")
        print(
            f"\n=== REPLY [{out.status}"
            f"{', ticket ' + out.ticket_id if out.ticket_id else ''}]\n{out.reply_text}"
        )
        if st.get("triage"):
            t = st["triage"]
            print(
                f"\n--- triage {t['tier']} (rules {t['rules_tier']}, llm {t['llm_tier']}), "
                f"urgency {t['urgency']}"
            )
        if st.get("entitlement"):
            e = st["entitlement"]
            print(
                f"--- entitlement {e['regime']}: EUR {e['compensation_eur']} ({e['confidence']}); "
                f"gold EUR {s.gold_entitlement.compensation_eur}"
            )
        for a in st.get("attempts") or []:
            failed = [c["check_id"] for c in a["code_checks"] if not c["passed"]]
            print(f"--- draft {a['attempt_no']} by {a['model']}: failed checks {failed or 'none'}")
        for action in st.get("actions") or []:
            print(f"--- action: {action}")
        if st.get("ticket"):
            print(
                f"--- handoff: {st['ticket']['reason']}; "
                f"next step: {st['ticket']['recommended_next_step']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
