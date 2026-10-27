import json
from typing import Any

import pytest

from flightright.config import RoleConfig
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.types import ProviderError
from flightright.llm.usage import UsageLedger
from flightright.triage.evaluation import TriageMetrics
from flightright.triage.rules import TriageContext, rule_triage
from flightright.triage.triage import ROLE, Triage, instructions, tier_definitions

TODAY = TriageContext(disruption_type="delay", days_until_departure=0, departure_delay_minutes=200)


@pytest.mark.parametrize(
    ("message", "tier"),
    [
        ("My flight is delayed, can you tell me what happens next?", "S2"),
        ("I am at the gate and my insulin is running out in a few hours.", "S4"),
        ("My 9 year old son is alone at the airport, flight cancelled!", "S4"),
        ("A man keeps harassing me at the gate and I feel unsafe.", "S4"),
        ("I'm shaking and losing my mind, I don't know what to do.", "S4"),
        ("We are stranded with nowhere to sleep tonight.", "S3"),
        ("I use a wheelchair and nobody is helping me.", "S3"),
        ("My connection leaves in 40 minutes, please help.", "S3"),
        ("This is unacceptable!!! Fix this right now, useless airline!", "S3"),
    ],
)
def test_FR_3_1_rule_signals(message: str, tier: str) -> None:
    assert rule_triage(message, TODAY).tier == tier


@pytest.mark.parametrize(
    "message",
    [
        "Flight delayed 3 hours. Do I get a hotel voucher or food?",
        "I will miss my connection in Frankfurt, what are my options?",
        "I'm stranded here at the gate, any update tonight?",
    ],
)
def test_ordinary_disruption_words_alone_stay_s2(message: str) -> None:
    assert rule_triage(message, TODAY).tier == "S2"


def test_past_and_far_future_flights_are_informational() -> None:
    past = TriageContext(disruption_type="delay", days_until_departure=-10)
    future = TriageContext(disruption_type="cancellation", days_until_departure=20)
    assert rule_triage("My flight arrived 4 hours late, can I claim?", past).tier == "S1"
    assert (
        rule_triage("My flight next month was cancelled, what are my options?", future).tier == "S1"
    )
    # stranded words in a past message do not make it urgent
    assert (
        rule_triage("Last week we were stranded overnight. Can I still claim?", past).tier == "S1"
    )


def test_FR_3_3_known_booking_flags_and_repeat_contacts_raise_the_tier() -> None:
    assert (
        rule_triage("Flight delayed.", TODAY.model_copy(update={"booking_flags": ["infant"]})).tier
        == "S3"
    )
    r = rule_triage("Any news?", TODAY.model_copy(update={"repeat_contact_count": 3}))
    assert r.tier == "S3" and any("repeat" in x for x in r.reasons)
    assert rule_triage("This is the third time I write about this.", TODAY).tier == "S3"


def test_urgency_and_deadline() -> None:
    r = rule_triage("My connection leaves in 2 hours.", TODAY)
    assert r.urgency == "immediate" and r.deadline_minutes == 120
    assert rule_triage("Flight delayed, any update?", TODAY).urgency == "soon"
    assert rule_triage("Medicine finish soon, I need my pills now", TODAY).urgency == "immediate"


def test_frustration_levels() -> None:
    assert rule_triage("Hello, flight delayed.", TODAY).frustration == 2
    assert rule_triage("This is ridiculous.", TODAY).frustration == 4
    assert rule_triage("WHY IS NOBODY ANSWERING THIS IS A JOKE", TODAY).frustration == 5


# ------------------------------------------------------------------ merge with the LLM


def _triage(*replies: str | Exception) -> Triage:
    cfg = models_cfg(roles={ROLE: RoleConfig(chain=["a:m1"], max_tokens=100)})
    provider = ScriptedProvider("a", list(replies))  # type: ignore[arg-type]
    client = LLMClient(
        cfg, {"a": provider}, LLMCache(":memory:"), UsageLedger(":memory:", cfg.catalog, 1.0, 0)
    )
    return Triage(client)


def _llm(**kw: Any) -> str:
    base = {
        "tier": "S2",
        "urgency": "soon",
        "frustration": 2,
        "vulnerability_flags": [],
        "reasons": ["delay"],
        "confidence": 0.9,
    }
    return json.dumps(base | kw)


def test_FR_3_2_llm_higher_tier_wins() -> None:
    res = _triage(_llm(tier="S3", vulnerability_flags=["pregnant"], reasons=["pregnant"])).classify(
        "Flight delayed, I am tired.", TODAY
    )
    assert res.tier == "S3" and res.source == "merged" and "pregnant" in res.vulnerability_flags
    assert (
        res.rules_tier == "S2"
        and res.llm_tier == "S3"
        and any("disagreement" in r for r in res.reasons)
    )


def test_FR_3_2_rules_higher_tier_wins_and_low_confidence_noted() -> None:
    res = _triage(_llm(tier="S2", confidence=0.4)).classify(
        "My insulin is running out soon.", TODAY
    )
    assert res.tier == "S4" and res.urgency == "immediate"
    assert any("low LLM confidence" in r for r in res.reasons)


def test_merged_deadline_takes_the_earliest() -> None:
    res = _triage(_llm(tier="S3", deadline_minutes=30, urgency="immediate")).classify(
        "My connection leaves in 2 hours.", TODAY
    )
    assert res.urgency_deadline_minutes == 30 and res.urgency == "immediate"


def test_llm_unavailable_falls_back_to_rules() -> None:
    res = _triage(ProviderError("down")).classify("We are stranded overnight.", TODAY)  # type: ignore[arg-type]
    assert (
        res.source == "rules" and res.tier == "S3" and any("unavailable" in r for r in res.reasons)
    )


def test_rules_only_without_client() -> None:
    assert Triage(None).classify("Flight delayed.", TODAY).source == "rules"


def test_instructions_include_all_tiers() -> None:
    text = instructions()
    assert all(t in text for t in ("S1", "S2", "S3", "S4")) and "injection" in text
    assert "medical need" in tier_definitions()


# ------------------------------------------------------------------ metrics


def test_metrics() -> None:
    m = TriageMetrics()
    for gold, pred in [("S3", "S3"), ("S3", "S2"), ("S4", "S4"), ("S2", "S3"), ("S1", "S1")]:
        m.add(gold, pred)
    assert m.n == 5 and m.accuracy == pytest.approx(0.6)
    assert m.recall_s3_plus == pytest.approx(2 / 3)
    assert m.under_triage_rate == pytest.approx(0.2) and m.over_triage_rate == pytest.approx(0.2)
    assert m.precision("S3") == pytest.approx(0.5) and m.recall("S2") == 0.0
    assert "| **S3** |" in m.table()
    assert TriageMetrics().recall_s3_plus == 1.0 and TriageMetrics().accuracy == 0.0
