"""Simulator (FR-13): CrewAI personas on a scripted client, the handoff oracle, multi-turn runs."""

from datetime import UTC, datetime

from flightright.agent.testing import GOOD_DRAFT, SUMMARY, build_harness, case
from flightright.config import RoleConfig
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.types import ProviderError
from flightright.llm.usage import UsageLedger
from flightright.models import Scenario
from flightright.rules.engine import EntitlementEngine
from flightright.scenarios.design import PlanConfig, build_plan
from flightright.simulator import oracle
from flightright.simulator.crew import END, ClientCrewLLM, next_message, passenger_agent
from flightright.simulator.runner import simulate

NOW = datetime(2027, 11, 15, 12, tzinfo=UTC)


def sim_llm(script: list[str | Exception]) -> tuple[ClientCrewLLM, ScriptedProvider]:
    cfg = models_cfg(roles={"simulator": RoleConfig(chain=["a:m1"], max_tokens=50)})
    provider = ScriptedProvider("a", script)  # type: ignore[arg-type]
    client = LLMClient(
        cfg, {"a": provider}, LLMCache(":memory:"), UsageLedger(":memory:", cfg.catalog, 1.0, 0)
    )
    return ClientCrewLLM(client), provider


def scenario(engine: EntitlementEngine, **kw: object) -> Scenario:
    s = build_plan(engine, PlanConfig(n_base=60, counterfactual_groups=0, seed=5)).scenarios[0]
    base = s.model_copy(
        update={
            "booking_ref": "ABC123",
            "passenger_ref": "P1",
            "opening_message": "Delayed, help",
            "follow_ups": ["ask_compensation_amount"],
            "case": case(),
        }
    )
    return base.model_copy(update={"gold_entitlement": engine.compute_entitlement(base.case), **kw})


def test_FR_13_1_crewai_agent_speaks_through_our_client() -> None:
    llm, provider = sim_llm(["How much compensation do I get?"])
    agent = passenger_agent(llm, "Anna", "Anna is stranded in Frankfurt with her baby.")
    text = next_message(
        agent, [{"role": "agent", "text": "We are sorry."}], "ask_compensation_amount"
    )
    assert text == "How much compensation do I get?" and len(provider.requests) == 1
    sent = provider.requests[0].messages
    assert "Anna is stranded" in sent[0].content and "ask compensation amount" in sent[1].content
    assert "SUPPORT: We are sorry." in sent[1].content and not provider.requests[0].json_mode


def test_simulator_outage_ends_the_conversation_gracefully() -> None:
    llm, _ = sim_llm([ProviderError("down"), ProviderError("down")])
    reply = next_message(passenger_agent(llm, "Anna", "b"), [], None)
    assert reply.startswith(END) and "unavailable" in reply


def test_crew_llm_accepts_plain_string_prompts() -> None:
    llm, provider = sim_llm(["ok"])
    assert llm.call("hello") == "ok" and provider.requests[0].messages[1].content == "hello"
    assert not llm.supports_function_calling()


def test_FR_13_2_oracle_resolves_from_ground_truth_with_optional_noise(
    engine: EntitlementEngine,
) -> None:
    s = scenario(engine)
    clean = oracle.resolve(s, NOW)
    assert clean.decision == "compensation_approved" and clean.compensation_paid_eur == 400
    noisy = [
        oracle.resolve(s.model_copy(update={"scenario_id": f"X{i}"}), NOW, noise=0.5, seed=1)
        for i in range(40)
    ]
    wrong = sum(r.decision == "compensation_denied" for r in noisy)
    assert 10 < wrong < 30
    assert oracle.resolve(s, NOW, noise=0.5, seed=1) == oracle.resolve(s, NOW, noise=0.5, seed=1)


def test_oracle_other_outcomes(engine: EntitlementEngine) -> None:
    out = scenario(
        engine, case=case(carrier_is_eu=False, origin_iata="JFK", final_destination_iata="CDG")
    )
    out = out.model_copy(update={"gold_entitlement": engine.compute_entitlement(out.case)})
    assert oracle.resolve(out, NOW).decision == "not_covered"
    canc = scenario(engine, case=case(disruption_type="cancellation", notice_given_days=20))
    canc = canc.model_copy(update={"gold_entitlement": engine.compute_entitlement(canc.case)})
    assert oracle.resolve(canc, NOW).decision == "rerouted_or_refunded"
    below = scenario(engine, case=case(arrival_delay_minutes=60, departure_delay_minutes=60))
    below = below.model_copy(update={"gold_entitlement": engine.compute_entitlement(below.case)})
    assert oracle.resolve(below, NOW).decision == "no_compensation"
    goodwill = [
        oracle.resolve(below.model_copy(update={"scenario_id": f"Y{i}"}), NOW, noise=1.0)
        for i in range(3)
    ]
    assert {r.decision for r in goodwill} == {"goodwill_payment"}


def test_FR_13_multi_turn_until_the_passenger_is_done(engine: EntitlementEngine) -> None:
    h = build_harness(engine, [GOOD_DRAFT, GOOD_DRAFT])
    llm, _ = sim_llm(["How much compensation exactly?", END])
    s = scenario(engine)
    out = simulate(s, h.service, h.start(), llm, max_turns=4)
    assert out.ended_by == "passenger_done" and out.n_turns == 2 and not out.handed_off
    assert (
        out.turns[1].passenger == "How much compensation exactly?" and out.turns[0].judge_decisions
    )


def test_handoff_is_resolved_by_the_oracle_and_remembered(engine: EntitlementEngine) -> None:
    h = build_harness(engine, [SUMMARY])
    llm, _ = sim_llm([])
    s = scenario(engine, opening_message="I want to speak to a human agent about my delayed flight")
    out = simulate(s, h.service, h.start(), llm)
    assert (
        out.ended_by == "handoff"
        and out.resolution
        and out.resolution.decision == "compensation_approved"
    )
    history = h.service.deps.tools.history.get("P1")
    assert (
        history.resolutions[0].compensation_paid_eur == 400
        and history.cases[0].status == "resolved"
    )


def test_max_turns_limit(engine: EntitlementEngine) -> None:
    h = build_harness(engine, [GOOD_DRAFT, GOOD_DRAFT])
    llm, _ = sim_llm(["Any news?"])
    out = simulate(scenario(engine), h.service, h.start(), llm, max_turns=2)
    assert out.ended_by == "max_turns" and out.n_turns == 2
