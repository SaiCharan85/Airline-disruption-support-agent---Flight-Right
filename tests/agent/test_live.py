from datetime import timedelta

from flightright.agent.judging import CodeChecksOnlyJudge
from flightright.agent.live import scenario_clock, service_for
from flightright.agent.testing import GOOD_DRAFT, SUMMARY
from flightright.config import RoleConfig, load_config
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.usage import UsageLedger
from flightright.rules.engine import EntitlementEngine
from flightright.scenarios.design import PlanConfig, build_plan
from flightright.triage.triage import Triage


def test_scenario_clock_follows_the_situation(engine: EntitlementEngine) -> None:
    plan = build_plan(engine, PlanConfig(n_base=60, counterfactual_groups=0, seed=3))
    for s in plan.scenarios[:30]:
        dep = s.case.scheduled_departure
        now = scenario_clock(s)
        if s.situation.timing == "past":
            assert now == dep + timedelta(days=7)
        elif s.situation.timing == "upcoming":
            assert now < dep
        else:
            assert dep <= now <= dep + timedelta(minutes=s.case.departure_delay_minutes)


def test_service_for_registers_booking_and_history(engine: EntitlementEngine) -> None:
    plan = build_plan(engine, PlanConfig(n_base=60, counterfactual_groups=0, seed=3))
    s = next(
        x
        for x in plan.scenarios
        if x.situation.repeat_contact_count >= 2 and x.case.disruption_type == "delay"
    )
    mcfg = models_cfg(
        roles={
            "agent": RoleConfig(chain=["a:m1"], max_tokens=50),
            "summary": RoleConfig(chain=["a:m1"], max_tokens=50),
        }
    )
    client = LLMClient(
        mcfg,
        {"a": ScriptedProvider("a", [GOOD_DRAFT] * 3 + [SUMMARY])},  # may revise, then hand off
        LLMCache(":memory:"),
        UsageLedger(":memory:", mcfg.catalog, 1.0, 0),
    )
    service, cid = service_for(
        s, load_config(), client, CodeChecksOnlyJudge(), engine, Triage(None)
    )
    assert service.deps.tools.airline.get_booking(s.booking_ref) is not None
    history = service.deps.tools.history.get(s.passenger_ref)
    assert len(history.contacts) == s.situation.repeat_contact_count
    service.handle_message(cid, s.opening_message, "m1")
    assert service.state(cid)["context"]["repeat_contacts"] == s.situation.repeat_contact_count
