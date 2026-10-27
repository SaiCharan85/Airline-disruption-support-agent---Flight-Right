"""The judge slot in the graph. The real severity-aware judge plugs in at Step 7b (FR-8).

The graph only depends on this protocol. Code-check failures are applied by the router before
the judge's opinion is considered, so no judge can approve a draft that failed a code check.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from flightright.models import CheckResult, Entitlement, TriageResult


class JudgeVerdict(BaseModel):
    decision: Literal["approve", "revise", "handoff"]
    verdict: Literal["good", "acceptable", "bad"] = "acceptable"
    revision_guidance: list[str] = Field(default_factory=list)
    tier_bumped_to: str | None = None
    judgement: dict[str, Any] = Field(default_factory=dict)  # full Judgement record once 7b lands


class Judge(Protocol):
    def evaluate(
        self,
        *,
        draft: str,
        transcript: list[dict[str, str]],
        triage: TriageResult,
        entitlement: Entitlement | None,
        checks: list[CheckResult],
        target: Literal["reply", "holding_message", "handoff_summary"],
    ) -> JudgeVerdict: ...


class CodeChecksOnlyJudge:
    """Placeholder until Step 7b: approves anything that passed the code checks."""

    def evaluate(
        self,
        *,
        draft: str,
        transcript: list[dict[str, str]],
        triage: TriageResult,
        entitlement: Entitlement | None,
        checks: list[CheckResult],
        target: Literal["reply", "holding_message", "handoff_summary"],
    ) -> JudgeVerdict:
        return JudgeVerdict(
            decision="approve",
            verdict="acceptable",
            judgement={"judge": "code_checks_only", "target": target},
        )
