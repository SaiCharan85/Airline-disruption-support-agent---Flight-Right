"""Evaluation runner: sampling, resumable drafting and judging, engine fairness."""

from pathlib import Path
from typing import Any

from flightright.agent.judging import JudgeVerdict
from flightright.agent.testing import ScriptedJudge, case
from flightright.eval.items import BaseReply, EvalItem
from flightright.eval.runner import (
    counterfactual_groups,
    draft_all,
    draft_until,
    engine_fairness,
    judge_all,
    read_jsonl,
    stratified,
)
from flightright.rules.engine import EntitlementEngine


class S:
    """Just enough of a Scenario for sampling."""

    def __init__(self, sid: str, tier: str, group: str | None = None) -> None:
        self.scenario_id, self.gold_tier, self.counterfactual_group = sid, tier, group
        self.case = case()


def scenarios() -> list[Any]:
    return (
        [S(f"A{i}", "S1") for i in range(5)]
        + [S(f"B{i}", "S3") for i in range(5)]
        + [S(f"C{i}", "S3", group=f"G{i % 2}") for i in range(4)]
    )


def test_stratified_is_seeded_and_skips_counterfactual_variants() -> None:
    a = stratified(scenarios(), {"S1": 2, "S3": 3}, seed=7)
    assert [s.gold_tier for s in a] == ["S1", "S1", "S3", "S3", "S3"]
    assert all(s.counterfactual_group is None for s in a)
    assert [s.scenario_id for s in stratified(scenarios(), {"S1": 2, "S3": 3}, 7)] == [
        s.scenario_id for s in a
    ]


def test_counterfactual_groups_keep_every_variant() -> None:
    g = counterfactual_groups(scenarios(), n_groups=1, seed=7)
    assert len(g) == 1 and len(next(iter(g.values()))) == 2


def test_draft_all_resumes_without_redrafting(tmp_path: Path) -> None:
    calls: list[str] = []

    def draft(s: Any) -> tuple[BaseReply | None, str]:
        calls.append(s.scenario_id)
        return None, "handed_off"

    path = tmp_path / "bases.jsonl"
    draft_all(scenarios()[:2], path, draft, log=lambda _: None)
    draft_all(scenarios()[:3], path, draft, log=lambda _: None)
    assert calls == ["A0", "A1", "A2"] and len(read_jsonl(path)) == 3


def base(engine: EntitlementEngine) -> BaseReply:
    return BaseReply(
        scenario_id="SC-1",
        split="dev",
        gold_tier="S3",
        draft="Sorry. EUR 400.",
        transcript=[{"role": "passenger", "text": "hi"}],
        triage={"tier": "S1", "urgency": "none", "frustration": 2, "source": "rules"},
        entitlement=engine.compute_entitlement(case()).model_dump(mode="json"),
        checks=[{"check_id": "amounts_match_entitlement", "passed": False, "detail": "x"}],
    )


def test_judge_all_uses_the_gold_tier_and_resumes(
    tmp_path: Path, engine: EntitlementEngine
) -> None:
    seen: list[Any] = []

    class Spy(ScriptedJudge):
        def evaluate(self, **kw: Any) -> JudgeVerdict:
            seen.append(kw)
            return JudgeVerdict(
                decision="revise",
                judgement={"decision_reason": "below the tier's bar", "model_calls": 3},
            )

    b = base(engine)
    items = [
        EvalItem(
            item_id="SC-1::original",
            base_id="SC-1",
            kind="original",
            tier="S3",
            reply=b.draft,
            checks=b.checks,
        )
    ]
    path = tmp_path / "judged.jsonl"
    rows = judge_all(items, {"SC-1": b}, path, Spy([]), log=lambda _: None)
    assert seen[0]["triage"].tier == "S3"  # gold tier, not the triage's S1
    assert rows[0]["decision"] == "revise" and rows[0]["requests"] == 3
    assert rows[0]["caught_by"] == "judge"
    judge_all(items, {"SC-1": b}, path, Spy([]), log=lambda _: None)
    assert len(seen) == 1  # resumed: not judged again


def test_judge_only_reports_every_check_as_passed(
    tmp_path: Path, engine: EntitlementEngine
) -> None:
    seen: list[Any] = []

    class Spy(ScriptedJudge):
        def evaluate(self, **kw: Any) -> JudgeVerdict:
            seen.append(kw)
            return JudgeVerdict(decision="approve", judgement={})

    b = base(engine)
    item = EvalItem(
        item_id="i", base_id="SC-1", kind="planted", tier="S2", reply=b.draft, checks=b.checks
    )
    judge_all(
        [item], {"SC-1": b}, tmp_path / "j.jsonl", Spy([]), judge_only=True, log=lambda _: None
    )
    assert all(c.passed for c in seen[0]["checks"])


def test_engine_fairness_flags_differing_entitlements(engine: EntitlementEngine) -> None:
    same = {"G": [S("x", "S2", "G"), S("y", "S2", "G")]}
    assert engine_fairness(same, engine)["groups_with_differences"] == 0
    other = S("z", "S2", "H")
    other.case = case(departure_delay_minutes=60, arrival_delay_minutes=60)
    diff = {"H": [S("w", "S2", "H"), other]}
    assert engine_fairness(diff, engine)["groups_with_differences"] == 1


def test_draft_until_replaces_unusable_cases_up_to_a_limit(tmp_path: Path) -> None:
    def draft(s: Any) -> tuple[BaseReply | None, str]:
        if s.scenario_id in {"A0", "A1", "A2", "A3"}:
            return None, "handed_off"
        return BaseReply.model_construct(scenario_id=s.scenario_id), "answered"

    rows = draft_until(scenarios(), {"S1": 1}, 0, tmp_path / "b.jsonl", draft, 3, lambda _: None)
    assert len(rows) <= 3  # at most 3x the target tried
    rows = draft_until(scenarios(), {"S1": 1}, 0, tmp_path / "c.jsonl", draft, 5, lambda _: None)
    assert rows[-1]["scenario_id"] == "A4" and sum(r["base"] is not None for r in rows) == 1


def test_judge_all_in_parallel_keeps_item_order(tmp_path: Path, engine: EntitlementEngine) -> None:
    class Approve(ScriptedJudge):
        def evaluate(self, **kw: Any) -> JudgeVerdict:
            return JudgeVerdict(decision="approve", judgement={"model_calls": 1})

    b = base(engine)
    items = [
        EvalItem(item_id=f"i{n}", base_id="SC-1", kind="original", tier="S2", reply=b.draft)
        for n in range(6)
    ]
    rows = judge_all(
        items, {"SC-1": b}, tmp_path / "p.jsonl", Approve([]), workers=3, log=lambda _: None
    )
    assert [r["item_id"] for r in rows] == [f"i{n}" for n in range(6)]
    assert len(read_jsonl(tmp_path / "p.jsonl")) == 6
