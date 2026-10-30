"""Evaluation items (docs/eval_protocol.md §3): planted errors, re-run code checks, bias probes."""

from collections import Counter

import pytest

from flightright.agent.testing import case
from flightright.eval.items import (
    BaseReply,
    CheckInputsRecord,
    bias_items,
    original,
    planted_items,
    rerun_checks,
)
from flightright.rules.engine import EntitlementEngine

REPLY = (
    "I'm very sorry your flight was cancelled, and that you have to contact us again about it.\n\n"
    "I've issued a meal voucher of EUR 20 for while you wait. The earliest option is XQ910 at "
    "11:20, or you can choose a full refund within 7 days.\n\n"
    "You are entitled to EUR 400 compensation. A colleague will confirm your rebooking today."
)


@pytest.fixture
def bases(engine: EntitlementEngine) -> list[BaseReply]:
    ent = engine.compute_entitlement(case()).model_dump(mode="json")
    out = []
    for i, tier in enumerate(["S1", "S2", "S3", "S3"]):
        b = BaseReply(
            scenario_id=f"SC-{i}",
            split="dev",
            gold_tier=tier,
            draft=REPLY,
            transcript=[{"role": "passenger", "text": "My flight is cancelled, second time I ask"}],
            triage={
                "tier": tier,
                "urgency": "immediate",
                "frustration": 4,
                "repeat_contact_count": 1,
                "source": "rules",
            },
            entitlement=ent,
            checks=[],
            check_inputs=CheckInputsRecord(voucher_amounts=[20], care_expected=True),
        )
        out.append(b.model_copy(update={"checks": rerun_checks(b, REPLY)}))
    return out


def test_base_reply_passes_its_own_code_checks(bases: list[BaseReply]) -> None:
    assert all(c["passed"] for c in bases[0].checks), bases[0].checks


def test_planted_items_two_per_reply_balanced_and_bad(bases: list[BaseReply]) -> None:
    items = planted_items(bases, per_base=2, seed=42)
    assert len(items) == 2 * len(bases)
    assert all(i.kind == "planted" and i.bad and i.expected_dims for i in items)
    assert Counter(i.base_id for i in items) == {b.scenario_id: 2 for b in bases}
    assert max(Counter(i.error_type for i in items).values()) == 1  # 8 picks, 13 types
    assert all(i.reply != REPLY for i in items)
    assert planted_items(bases, 2, 42) == items  # seeded


def test_wrong_amount_is_caught_by_the_rerun_code_checks(bases: list[BaseReply]) -> None:
    mutated = REPLY.replace("EUR 400", "EUR 600")
    failed = [c["check_id"] for c in rerun_checks(bases[0], mutated) if not c["passed"]]
    assert failed, "a misstated compensation amount must fail a code check"


def test_original_item_keeps_tier_and_checks(bases: list[BaseReply]) -> None:
    item = original(bases[2])
    assert item.kind == "original" and not item.bad and item.tier == "S3"
    assert item.checks == bases[2].checks


def test_bias_items_pad_without_and_with_a_hidden_error(bases: list[BaseReply]) -> None:
    items = bias_items(bases[1:3], seed=42)
    kinds = Counter(i.kind for i in items)
    assert kinds["verbosity"] == 2 and kinds["politeness_error"] >= 1
    pad = next(i for i in items if i.kind == "verbosity")
    assert pad.reply.startswith(REPLY.rstrip()) and len(pad.reply) > len(REPLY) and not pad.bad
    hidden = next(i for i in items if i.kind == "politeness_error")
    assert hidden.bad and hidden.error_type in ("wrong_regime", "fake_extraordinary")
    assert "grateful for your patience" in hidden.reply
