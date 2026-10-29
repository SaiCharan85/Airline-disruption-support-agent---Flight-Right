"""The severity-aware judge (FR-8) and the decision logic (SPEC §6.3), with scripted LLMs."""

import json
import random
from typing import Any

import pytest
from judgekit import Sample

from flightright.agent.testing import GOOD_DRAFT, SUMMARY, build_harness, case
from flightright.config import RoleConfig
from flightright.judge.judge import SeverityAwareJudge, load_airline_rubric, render_case
from flightright.judge.planted import ERROR_TYPES
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.types import ProviderError
from flightright.llm.usage import UsageLedger
from flightright.models import CheckResult, TriageResult
from flightright.rules.engine import EntitlementEngine

DRAFT = (
    "I'm so sorry your flight was cancelled and that you have been waiting with your baby. "
    "I've issued a meal voucher of EUR 20 for while you wait.\n\n"
    "The earliest option is XQ910, departing at 11:20. You can also choose a full refund.\n\n"
    "You are entitled to EUR 250 compensation. A colleague will confirm your choice."
)
DIMS = [
    "relevance",
    "context_awareness",
    "urgency",
    "tone",
    "grievance_handling",
    "correctness",
    "actionability",
    "fairness_safety",
]
CHECKS = {"urgency", "correctness", "fairness_safety"}


def answer(
    score: int = 5,
    fail: str | None = None,
    low: str | None = None,
    evidence: str = "I'm so sorry",
    **kw: Any,
) -> str:
    answers = []
    for d in DIMS:
        if d in CHECKS:
            answers.append({"id": d, "passed": d != fail, "evidence": evidence, "rationale": "r"})
        else:
            answers.append(
                {"id": d, "score": 2 if d == low else score, "evidence": evidence, "rationale": "r"}
            )
    return json.dumps(
        {
            "answers": answers,
            "label": kw.get("label", "good"),
            "analysis": "a",
            "revision_guidance": kw.get("guidance", ""),
        }
    )


def judge(
    a: list[Any], b: list[Any] | None = None
) -> tuple[SeverityAwareJudge, ScriptedProvider, ScriptedProvider]:
    cfg = models_cfg(
        roles={
            "judge_a": RoleConfig(chain=["a:m1"], max_tokens=100),
            "judge_b": RoleConfig(chain=["b:m2"], max_tokens=100),
            "agent": RoleConfig(chain=["a:m1"], max_tokens=100),
        }
    )
    pa, pb = ScriptedProvider("a", a), ScriptedProvider("b", b or [])
    client = LLMClient(
        cfg, {"a": pa, "b": pb}, LLMCache(":memory:"), UsageLedger(":memory:", cfg.catalog, 1.0, 0)
    )
    return SeverityAwareJudge(client, author_families=frozenset({"agent_family"})), pa, pb


def triage(tier: str = "S2", urgency: str = "soon") -> TriageResult:
    return TriageResult(
        tier=tier,
        urgency=urgency,
        frustration=3,
        source="rules",  # type: ignore[arg-type]
        vulnerability_flags=["infant"],
        repeat_contact_count=2,
        reasons=["test"],
    )


def run(
    j: SeverityAwareJudge,
    tier: str = "S2",
    checks: list[CheckResult] | None = None,
    target: str = "reply",
    engine: EntitlementEngine | None = None,
):  # type: ignore[no-untyped-def]
    ent = engine.compute_entitlement(case()) if engine else None
    return j.evaluate(
        draft=DRAFT,
        transcript=[{"role": "passenger", "text": "Cancelled, help"}],
        triage=triage(tier),
        entitlement=ent,
        checks=checks or [],
        target=target,
    )  # type: ignore[arg-type]


def test_rubric_matches_the_spec() -> None:
    r = load_airline_rubric()
    assert list(r.ids) == DIMS
    assert r.profile("S3").panel == 2 and set(r.profile("S3").must_pass) == {
        "urgency",
        "correctness",
        "fairness_safety",
    }
    assert r.profile("S1").min_scores == {"relevance": 3, "tone": 3}


def test_FR_8_2_case_contains_the_full_context(engine: EntitlementEngine) -> None:
    text = render_case(
        transcript=[{"role": "passenger", "text": "Where is my bag of patience?"}],
        triage=triage("S3"),
        entitlement=engine.compute_entitlement(case()),
        checks=[CheckResult(check_id="care_mentioned", passed=False, detail="missing")],
        target="reply",
    )
    for part in (
        "Severity: S3",
        "infant",
        "earlier contacts about this booking: 2",
        "EUR 400",
        "FAILED: care_mentioned",
        "Where is my bag of patience?",
    ):
        assert part in text


def test_FR_8_5_good_reply_is_approved_and_recorded(engine: EntitlementEngine) -> None:
    j, pa, _ = judge([answer()])
    v = run(j, engine=engine)
    assert v.decision == "approve" and v.verdict == "good"
    rec = v.judgement
    assert (
        rec["severity_tier"] == "S2"
        and rec["rubric_id"] == "airline_v1@1.0"
        and len(rec["scores"]) == 8
    )
    assert DRAFT in pa.requests[0].messages[1].content


def test_score_below_the_bar_revises_with_guidance(engine: EntitlementEngine) -> None:
    j, _, _ = judge([answer(low="tone", guidance="Acknowledge the baby.")])
    v = run(j, engine=engine)
    assert v.decision == "revise" and "tone: score 2 below 3" in v.revision_guidance
    assert "Acknowledge the baby." in v.revision_guidance


def test_must_pass_failure_revises(engine: EntitlementEngine) -> None:
    j, _, _ = judge([answer(fail="correctness")])
    assert run(j, engine=engine).decision == "revise"


def test_s1_bar_is_lower_than_s3(engine: EntitlementEngine) -> None:
    j, _, _ = judge([answer(score=3)])
    assert run(j, tier="S1", engine=engine).decision == "approve"
    j3, _, _ = judge([answer(score=3)], [answer(score=3)])
    assert run(j3, tier="S3", engine=engine).decision == "revise"


def test_FR_8_6_s3_uses_a_two_family_panel(engine: EntitlementEngine) -> None:
    j, pa, pb = judge([answer()], [answer()])
    v = run(j, tier="S3", engine=engine)
    assert v.decision == "approve" and len(pa.requests) == 1 and len(pb.requests) == 1
    assert v.judgement["panel"]["members"] == ["judge_a", "judge_b"]


def test_FR_8_6_panel_disagreement_hands_off(engine: EntitlementEngine) -> None:
    j, _, _ = judge([answer()], [answer(low="grievance_handling")])
    v = run(j, tier="S3", engine=engine)
    assert v.decision == "handoff" and v.judgement["panel"]["disagreement"]
    assert v.judgement["decision_reason"] == "panel disagreement"


def test_FR_7_6_failed_code_check_revises_without_calling_the_judge() -> None:
    j, pa, _ = judge([])
    v = run(
        j,
        checks=[CheckResult(check_id="amounts_match_entitlement", passed=False, detail="EUR 999")],
    )
    assert (
        v.decision == "revise"
        and pa.requests == []
        and "amounts_match_entitlement" in v.revision_guidance[0]
    )


def test_FR_8_4_invented_evidence_is_retried_then_hands_off(engine: EntitlementEngine) -> None:
    j, pa, _ = judge([answer(evidence="words not in the draft"), answer(evidence="still invented")])
    v = run(j, engine=engine)
    assert (
        v.decision == "handoff"
        and "evidence not found" in v.judgement["error"]
        and len(pa.requests) == 2
    )


def test_NFR_3_judge_outage_hands_off(engine: EntitlementEngine) -> None:
    j, _, _ = judge([ProviderError("down"), ProviderError("down")])
    v = run(j, engine=engine)
    assert v.decision == "handoff" and "judge call failed" in v.judgement["error"]


def test_FR_8_8_judge_avoids_the_agent_family(engine: EntitlementEngine) -> None:
    j, _, _ = judge([answer()])
    assert j.author_families == frozenset({"agent_family"})
    cfg = models_cfg(
        roles={
            "judge_a": RoleConfig(chain=["a:m1"]),
            "judge_b": RoleConfig(chain=["b:m2"]),
            "agent": RoleConfig(chain=["a:m1"]),
        }
    )
    client = LLMClient(
        cfg,
        {"a": ScriptedProvider("a", [answer()])},
        LLMCache(":memory:"),
        UsageLedger(":memory:", cfg.catalog, 1.0, 0),
    )
    same_family = SeverityAwareJudge.for_client(client)  # agent and judge_a share fam1
    v = run(same_family, engine=engine)
    assert same_family.author_families == frozenset({"fam1"}) and v.decision == "handoff"


@pytest.mark.parametrize("target", ["holding_message", "handoff_summary"])
def test_FR_9_2_holding_messages_and_summaries_use_the_s4_profile(target: str) -> None:
    j, _, _ = judge([answer(score=1)])
    v = run(j, tier="S2", target=target)
    assert (
        v.judgement["severity_tier"] == "S4" and v.decision == "approve"
    )  # only correctness / safety


def test_exemplars_are_passed_to_the_judge(engine: EntitlementEngine) -> None:
    from judgekit import Exemplar

    j, pa, _ = judge([answer()])
    j.exemplars = lambda tier, target: [Exemplar(dimension="tone", excerpt="Great news!", score=1)]
    v = run(j, engine=engine)
    assert (
        "Calibration examples" in pa.requests[0].messages[1].content
        and v.judgement["exemplar_count"] == 1
    )


# ---------------------------------------------------------------- planted errors (SPEC §10.1)


SAMPLE = Sample(
    id="s1",
    fields={
        "reply": "I'm so sorry you have had to contact us again about this, and that you are "
        "waiting with your baby. I've issued a meal voucher of EUR 20 right away.\n\n"
        "The earliest flight leaves at 11:20. "
        "If you prefer, you can have a full refund by bank transfer.\n\nYou are owed EUR 250 "
        "compensation. A colleague will confirm your choice today.",
        "regime": "eu261_2004",
        "urgency": "immediate",
        "repeat_contacts": "2",
    },
)


@pytest.mark.parametrize("error", ERROR_TYPES, ids=[e.name for e in ERROR_TYPES])
def test_every_planted_error_changes_the_reply_and_names_dimensions(error) -> None:  # type: ignore[no-untyped-def]
    mutation = error.mutate(SAMPLE, random.Random(0))
    assert mutation is not None, error.name
    assert mutation.changes["reply"] != SAMPLE.fields["reply"] and mutation.description
    assert error.expected_items <= set(DIMS)


def test_planted_errors_have_the_intended_effect() -> None:
    by = {e.name: e for e in ERROR_TYPES}

    def plant(name: str) -> str:
        m = by[name].mutate(SAMPLE, random.Random(0))
        assert m is not None
        return m.changes["reply"]

    assert "EUR 250" not in plant("wrong_amount")
    assert "bank transfer" not in plant("voucher_push") and "travel voucher" in plant(
        "voucher_push"
    )
    assert "meal" not in plant("missing_care")
    assert plant("cheerful_in_crisis").startswith("Great news") and "sorry" not in plant(
        "cheerful_in_crisis"
    )
    assert plant("buried_urgency").startswith("Thank you for contacting us")
    assert "again" not in plant("ignore_repeat_contact")
    assert "11:20" not in plant("partial_answer") and "colleague" not in plant("no_next_step")


def test_planted_errors_skip_samples_they_do_not_fit() -> None:
    plain = Sample(
        id="p", fields={"reply": "Short reply.", "urgency": "none", "repeat_contacts": "0"}
    )
    rng = random.Random(0)
    for name in (
        "wrong_amount",
        "missing_care",
        "buried_urgency",
        "generic_apology",
        "ignore_repeat_contact",
        "partial_answer",
        "no_next_step",
    ):
        assert next(e for e in ERROR_TYPES if e.name == name).mutate(plain, rng) is None, name


# ---------------------------------------------------------------- inside the graph


def test_graph_with_the_real_judge_revises_then_sends(engine: EntitlementEngine) -> None:
    quote = "I'm sorry your flight is delayed"  # appears in GOOD_DRAFT
    rubric_judge, _, _ = judge(
        [answer(low="tone", guidance="Warmer, please.", evidence=quote), answer(evidence=quote)]
    )
    revised = GOOD_DRAFT + " We know how stressful this is."
    h = build_harness(engine, [GOOD_DRAFT, revised], judge=rubric_judge)
    cid = h.start()
    out = h.service.handle_message(cid, "My flight is delayed, help", "m1")
    state = h.service.state(cid)
    assert out.status == "answered" and len(state["attempts"]) == 2
    assert "Warmer, please." in h.provider.requests[1].messages[1].content
    assert [v["judgement"]["decision"] for v in state["verdicts"]] == ["revise", "approve"]


def test_graph_hands_off_when_the_judge_is_down(engine: EntitlementEngine) -> None:
    rubric_judge, _, _ = judge([ProviderError("x")] * 4)
    h = build_harness(engine, [GOOD_DRAFT, SUMMARY], judge=rubric_judge)
    cid = h.start()
    out = h.service.handle_message(cid, "My flight is delayed, help", "m1")
    assert (
        out.status == "handed_off" and h.service.state(cid)["ticket"]["reason"] == "judge_handoff"
    )
