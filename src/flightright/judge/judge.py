"""The severity-aware, context-reading judge (FR-8) on JudgeKit scored rubrics.

Per draft: pick the profile from the triage tier (S1-S3 replies; S4 for holding messages and handoff
summaries), run one judge or a two-judge panel (S3), and decide by code (SPEC §6.3):

1. a failed code check -> revise          (the LLM judge can never override it)
2. no trustworthy verdict -> handoff      (judge unavailable or invented evidence, after one retry)
3. must-pass check failed / score below the tier's bar -> revise
4. S3 panel members disagree on pass/fail -> handoff
5. otherwise -> approve

The judge's label (good / acceptable / bad) is reported but never decides.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from judgekit import (
    Decision,
    Exemplar,
    ScoredLLMJudge,
    ScoredPanel,
    ScoredRubric,
    ScoredVerdict,
    decide,
    load_scored_rubric,
)

from flightright.agent.judging import JudgeVerdict
from flightright.config import ROOT
from flightright.judge.adapter import ClientJudgeLLM
from flightright.llm.client import LLMClient
from flightright.models import CheckResult, Entitlement, TriageResult

Target = Literal["reply", "holding_message", "handoff_summary"]
PROMPT_VERSION = "judge-prompt-1"
ExemplarProvider = Callable[[str, str], Sequence[Exemplar]]  # (tier, target) -> exemplars


def load_airline_rubric(path: Path | None = None) -> ScoredRubric:
    return load_scored_rubric(path or ROOT / "config" / "rubrics" / "airline_v1.yaml")


def render_case(
    *,
    transcript: list[dict[str, str]],
    triage: TriageResult,
    entitlement: Entitlement | None,
    checks: list[CheckResult],
    target: Target,
) -> str:
    """Everything the judge reads besides the draft itself (FR-8.2)."""
    e = entitlement
    lines = [
        f"What is being judged: {target.replace('_', ' ')}",
        f"Severity: {triage.tier}; urgency: {triage.urgency}; frustration {triage.frustration}/5; "
        f"special needs: {', '.join(triage.vulnerability_flags) or 'none'}; "
        f"earlier contacts about this booking: {triage.repeat_contact_count}",
        f"Triage reasons: {'; '.join(triage.reasons[:6]) or 'none'}",
    ]
    if e is not None:
        comp = (
            f"EUR {e.compensation_eur}"
            + (f" (may be reduced to EUR {e.may_reduce_to_eur})" if e.may_reduce_to_eur else "")
            if e.compensation_eur and e.confidence == "verified"
            else "needs review (no amount may be stated)"
            if e.confidence == "unverified"
            else "none"
        )
        lines += [
            "What the passenger is owed (rules engine; correct by definition):",
            f"- regime {e.regime}; in scope: {e.in_scope}; "
            f"compensation: {comp} ({e.compensation_reason})",
            "- care: " + ("; ".join(f"{c.item} ({c.detail})" for c in e.care) or "none"),
            "- refund/rerouting: "
            + (
                ", ".join(e.refund_or_rerouting.options)
                if e.refund_or_rerouting
                else "not applicable"
            ),
            "- travel vouchers only with consent and only alongside the cash/refund right",
        ]
    failed = [c for c in checks if not c.passed]
    lines.append(
        "Automatic fact checks: "
        + (
            "all passed"
            if not failed
            else "FAILED: " + "; ".join(f"{c.check_id} ({c.detail})" for c in failed)
        )
    )
    lines.append("Conversation so far:")
    lines += [f"{t['role'].upper()}: {t['text']}" for t in transcript[-8:]]
    return "\n".join(lines)


class SeverityAwareJudge:
    """Implements the graph's `Judge` protocol (flightright.agent.judging)."""

    def __init__(
        self,
        client: LLMClient,
        rubric: ScoredRubric | None = None,
        *,
        author_families: frozenset[str] = frozenset(),
        exemplars: ExemplarProvider | None = None,
        use_cache: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.rubric = rubric or load_airline_rubric()
        self.author_families = (
            author_families  # FR-8.8: never judged by the agent's own model family
        )
        self.exemplars = exemplars
        self.clock = clock
        self.judge_a = ScoredLLMJudge(
            ClientJudgeLLM(client, "judge_a", use_cache), self.rubric, name="judge_a"
        )
        self.judge_b = ScoredLLMJudge(
            ClientJudgeLLM(client, "judge_b", use_cache), self.rubric, name="judge_b"
        )
        self.panel = ScoredPanel(
            [self.judge_a, self.judge_b], self.rubric, combine="min", name="panel_s3"
        )

    @classmethod
    def for_client(cls, client: LLMClient, **kw: Any) -> SeverityAwareJudge:
        """Author families = the families in the agent role's chain."""
        families = frozenset(client.family_of(k) for k in client.cfg.roles["agent"].chain)
        return cls(client, author_families=families, **kw)

    @staticmethod
    def profile_for(triage: TriageResult, target: Target) -> str:
        return "S4" if target != "reply" or triage.tier == "S4" else triage.tier

    def evaluate(
        self,
        *,
        draft: str,
        transcript: list[dict[str, str]],
        triage: TriageResult,
        entitlement: Entitlement | None,
        checks: list[CheckResult],
        target: Target,
    ) -> JudgeVerdict:
        start = time.perf_counter()
        profile = self.profile_for(triage, target)
        failed_checks = [c for c in checks if not c.passed]
        if failed_checks and target == "reply":
            # The outcome is "revise" whatever the model says: skip the call (free-tier frugality).
            return JudgeVerdict(
                decision="revise",
                verdict="bad",
                revision_guidance=[f"{c.check_id}: {c.detail}" for c in failed_checks],
                judgement={
                    "judgement_id": "J-" + uuid.uuid4().hex[:12],
                    "target": target,
                    "severity_tier": profile,
                    "verdict": "bad",
                    "decision": "revise",
                    "decision_reason": "code check failed (judge not called)",
                    "scores": [],
                    "failures": [
                        {"dimension": "correctness", "reason": c.check_id} for c in failed_checks
                    ],
                    "rubric_id": f"{self.rubric.name}@{self.rubric.version}",
                    "prompt_version": PROMPT_VERSION,
                    "cost_usd": 0.0,
                    "latency_ms": 0,
                    "created_at": self.clock().isoformat(),
                },
            )
        case = render_case(
            transcript=transcript,
            triage=triage,
            entitlement=entitlement,
            checks=checks,
            target=target,
        )
        exemplars = list(self.exemplars(profile, target)) if self.exemplars else []
        panel_size = self.rubric.profile(profile).panel
        disagreement = False
        decisions: tuple[Decision, ...] = ()
        if panel_size >= 2:
            outcome = self.panel.run(
                case,
                draft,
                profile=profile,
                avoid_families=self.author_families,
                exemplars=exemplars,
            )
            verdict, disagreement, decisions = (
                outcome.verdict,
                outcome.disagreement,
                outcome.decisions,
            )
        else:
            verdict = self.judge_a.evaluate(
                case, draft, avoid_families=self.author_families, exemplars=exemplars
            )
        decision = decide(verdict, self.rubric, profile)

        if failed_checks:  # holding messages / summaries with a failed check
            route, why = "revise", "code check failed"
        elif not verdict.available:
            route, why = "handoff", f"no trustworthy verdict: {verdict.error}"
        elif disagreement:
            route, why = "handoff", "panel disagreement"
        elif not decision.passed:
            route, why = "revise", "below the tier's bar"
        else:
            route, why = "approve", "meets the tier's bar"

        guidance = [f"{f.dimension}: {f.reason}" for f in decision.failures if f.dimension != "*"]
        if verdict.revision_guidance:
            guidance.append(verdict.revision_guidance)
        record = self._judgement(
            verdict,
            decision,
            profile,
            target,
            route,
            why,
            disagreement,
            decisions,
            exemplars,
            time.perf_counter() - start,
        )
        return JudgeVerdict(
            decision=route,  # type: ignore[arg-type]
            verdict=verdict.label,
            revision_guidance=guidance if route == "revise" else [],
            judgement=record,
        )

    def _judgement(
        self,
        verdict: ScoredVerdict,
        decision: Decision,
        profile: str,
        target: Target,
        route: str,
        why: str,
        disagreement: bool,
        member_decisions: tuple[Decision, ...],
        exemplars: Sequence[Exemplar],
        latency_s: float,
    ) -> dict[str, Any]:
        """The Judgement record (SPEC §5), JSON-ready for state, ops API and Judge Monitor."""
        return {
            "judgement_id": "J-" + uuid.uuid4().hex[:12],
            "target": target,
            "severity_tier": profile,
            "verdict": verdict.label,
            "decision": route,
            "decision_reason": why,
            "failures": [f.model_dump() for f in decision.failures],
            "scores": [r.model_dump() for r in verdict.results],
            "analysis": verdict.analysis,
            "revision_guidance": verdict.revision_guidance or None,
            "panel": {
                "members": [m.judge_id for m in verdict.members],
                "disagreement": disagreement,
                "member_passed": [d.passed for d in member_decisions],
            }
            if verdict.members
            else None,
            "models": {c.model: c.family for c in verdict.calls},
            "error": verdict.error or None,
            "rubric_id": f"{self.rubric.name}@{self.rubric.version}",
            "prompt_version": PROMPT_VERSION,
            "exemplar_count": len(exemplars),
            "cost_usd": 0.0,
            "latency_ms": int(latency_s * 1000),
            "created_at": self.clock().isoformat(),
        }
