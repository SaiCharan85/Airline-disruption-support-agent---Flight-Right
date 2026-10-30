"""Evaluation metrics (SPEC §10.2) on hand-made judged items."""

from typing import Any

import pytest

from flightright.eval.metrics import (
    catch_rates,
    decision_rule,
    fairness_gaps,
    mcnemar_exact,
    pair_accuracy,
    rate,
    requests_and_latency,
    s1_overblocking,
    score_shift,
    severe_leak,
    spearman,
    wilson,
)


def scores(**kw: Any) -> list[dict[str, Any]]:
    out = []
    for dim, v in kw.items():
        if isinstance(v, bool):
            out.append({"id": dim, "kind": "check", "passed": v})
        else:
            out.append({"id": dim, "kind": "scale", "score": v})
    return out


def judged(kind: str, tier: str, decision: str, base: str = "B1", **kw: Any) -> dict[str, Any]:
    return {
        "item_id": f"{base}::{kind}::{kw.get('error_type', '')}",
        "base_id": base,
        "kind": kind,
        "tier": tier,
        "decision": decision,
        "error_type": kw.get("error_type"),
        "expected_dims": kw.get("expected_dims", []),
        "caught_by": kw.get("caught_by", "judge"),
        "scores": kw.get("scores", []),
        "requests": kw.get("requests", 1),
        "latency_ms": kw.get("latency_ms", 1000),
    }


def test_wilson_interval_brackets_the_rate() -> None:
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi and 0.18 < lo < 0.25
    assert wilson(0, 0) == (0.0, 1.0)
    assert rate(1, 4)["rate"] == 0.25


def test_severe_leak_counts_approved_s3_planted_only() -> None:
    items = [
        judged("planted", "S3", "approve"),
        judged("planted", "S3", "revise"),
        judged("planted", "S2", "approve"),  # not severe
        judged("original", "S3", "approve"),  # not bad
    ]
    leak = severe_leak(items)
    assert leak["k"] == 1 and leak["n"] == 2


def test_s1_overblocking_counts_non_approved_good_s1() -> None:
    items = [
        judged("original", "S1", "approve"),
        judged("original", "S1", "revise"),
        judged("original", "S1", "handoff"),
        judged("planted", "S1", "revise"),
    ]
    assert s1_overblocking(items)["rate"] == pytest.approx(2 / 3)


def test_catch_rates_by_type_dimension_tier_and_code() -> None:
    items = [
        judged(
            "planted",
            "S2",
            "revise",
            error_type="wrong_amount",
            expected_dims=["correctness"],
            caught_by="code",
        ),
        judged("planted", "S3", "approve", error_type="cheerful_in_crisis", expected_dims=["tone"]),
        judged(
            "planted",
            "S3",
            "handoff",
            error_type="generic_apology",
            expected_dims=["tone", "grievance_handling"],
        ),
    ]
    c = catch_rates(items)
    assert c["overall"]["k"] == 2 and c["overall"]["by_code"] == 1
    assert c["dimension"]["tone"]["k"] == 1 and c["dimension"]["tone"]["n"] == 2
    assert c["tier"]["S3"]["rate"] == 0.5


def test_pair_accuracy_needs_a_strict_win_on_an_affected_dimension() -> None:
    orig = judged("original", "S2", "approve", scores=scores(tone=5, correctness=True))
    items = [
        orig,
        judged(
            "planted",
            "S2",
            "revise",
            error_type="cheerful_in_crisis",
            expected_dims=["tone"],
            scores=scores(tone=2, correctness=True),
        ),
        judged(
            "planted",
            "S2",
            "approve",
            error_type="generic_apology",
            expected_dims=["tone"],
            scores=scores(tone=5, correctness=True),
        ),  # tie = miss
        judged(
            "planted",
            "S2",
            "revise",
            error_type="wrong_regime",
            expected_dims=["correctness"],
            scores=scores(tone=5, correctness=False),
        ),
        judged(
            "planted",
            "S2",
            "revise",
            error_type="wrong_amount",
            expected_dims=["correctness"],
            caught_by="code",
        ),  # no scores: skipped
    ]
    acc = pair_accuracy(items)
    assert acc["overall"]["k"] == 2 and acc["overall"]["n"] == 3
    assert acc["skipped_code_caught"] == 1


def test_mcnemar_exact_two_sided() -> None:
    ours = [True] * 8 + [False] * 2
    theirs = [False] * 8 + [False] * 2
    m = mcnemar_exact(ours, theirs)
    assert m["b"] == 8 and m["c"] == 0 and m["p"] == pytest.approx(2 / 256)
    assert mcnemar_exact([True], [True])["p"] == 1.0


def test_decision_rule_outcomes() -> None:
    def side(leak: float, over: float, req: float) -> dict[str, Any]:
        return {"leak": {"rate": leak}, "overblock": {"rate": over}, "requests": req}

    assert decision_rule(side(0.05, 0.10, 1.5), side(0.30, 0.08, 1.0))["outcome"] == "better"
    assert decision_rule(side(0.05, 0.40, 1.5), side(0.30, 0.08, 1.0))["outcome"] == "trade-off"
    assert decision_rule(side(0.05, 0.10, 2.5), side(0.30, 0.08, 1.0))["outcome"] == "trade-off"
    assert (
        decision_rule(side(0.28, 0.10, 1.5), side(0.30, 0.08, 1.0))["outcome"] == "no improvement"
    )
    assert decision_rule(side(0.0, 0.0, 1.0), side(0.01, 0.0, 1.0))["outcome"] == "better"


def test_spearman_with_ties() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_score_shift_matches_by_base() -> None:
    o = [judged("original", "S2", "approve", base="B1", scores=scores(tone=3, relevance=4))]
    p = [judged("verbosity", "S2", "approve", base="B1", scores=scores(tone=4, relevance=4))]
    shift = score_shift(o, p)
    assert shift["tone"]["mean_shift"] == 1 and shift["relevance"]["mean_shift"] == 0


def test_fairness_gaps() -> None:
    groups = {
        "G1": [
            judged("original", "S2", "approve", scores=scores(tone=5, relevance=5)),
            judged("original", "S2", "revise", scores=scores(tone=3, relevance=5)),
        ],
        "G2": [judged("original", "S2", "approve", scores=scores(tone=4))] * 2,
    }
    g = fairness_gaps(groups)
    assert g["groups"]["G1"]["score_gap"] == 1 and g["groups"]["G1"]["approval_gap"] == 1
    assert g["groups"]["G2"]["score_gap"] == 0 and g["groups_with_approval_gap"] == 1


def test_requests_and_latency_per_tier() -> None:
    items = [
        judged("original", "S3", "approve", requests=2, latency_ms=3000),
        judged("original", "S3", "approve", requests=4, latency_ms=5000),
        judged("original", "S1", "approve", requests=1, latency_ms=1000),
    ]
    r = requests_and_latency(items)
    assert r["S3"]["requests_mean"] == 3 and r["S3"]["requests_p95"] == 4
    assert r["all"]["n"] == 3
