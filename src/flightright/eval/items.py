"""Evaluation items (docs/eval_protocol.md §3): base replies, planted errors, bias probes.

A base reply is a draft produced by the real graph that passed every code check; it keeps what is
needed to re-run the code checks after a mutation. Items are what a judge is asked to evaluate.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any, Literal

from judgekit import Sample, plant
from pydantic import BaseModel, Field

from flightright.agent import checks as code_checks
from flightright.judge.planted import ERROR_TYPES
from flightright.models import CheckResult, Entitlement

Kind = Literal["original", "planted", "verbosity", "politeness_error"]


class CheckInputsRecord(BaseModel):
    voucher_amounts: list[int] = Field(default_factory=list)
    refund_amount: int | None = None
    care_expected: bool = False
    allowed_timeline_days: list[int] = Field(default_factory=lambda: [7])


class BaseReply(BaseModel):
    scenario_id: str
    split: str
    gold_tier: str
    draft: str
    transcript: list[dict[str, str]]
    triage: dict[str, Any]
    entitlement: dict[str, Any]
    checks: list[dict[str, Any]]
    check_inputs: CheckInputsRecord = Field(default_factory=CheckInputsRecord)
    counterfactual_group: str | None = None
    variant: str | None = None


class EvalItem(BaseModel):
    item_id: str
    base_id: str
    kind: Kind
    tier: str
    reply: str
    error_type: str | None = None
    expected_dims: list[str] = Field(default_factory=list)
    description: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def bad(self) -> bool:
        return self.kind in ("planted", "politeness_error")


def rerun_checks(base: BaseReply, reply: str) -> list[dict[str, Any]]:
    ci = base.check_inputs
    inp = code_checks.CheckInputs(
        entitlement=Entitlement.model_validate(base.entitlement),
        issued_voucher_amounts=set(ci.voucher_amounts),
        refund_amount=ci.refund_amount,
        care_expected=ci.care_expected,
        allowed_timeline_days=set(ci.allowed_timeline_days),
    )
    return [r.model_dump() for r in code_checks.run_checks(reply, inp)]


def sample_for(base: BaseReply) -> Sample:
    return Sample(
        id=base.scenario_id,
        fields={
            "reply": base.draft,
            "regime": str(base.entitlement.get("regime", "")),
            "urgency": str(base.triage.get("urgency", "none")),
            "repeat_contacts": str(base.triage.get("repeat_contact_count", 0)),
        },
    )


def original(base: BaseReply) -> EvalItem:
    return EvalItem(
        item_id=f"{base.scenario_id}::original",
        base_id=base.scenario_id,
        kind="original",
        tier=base.gold_tier,
        reply=base.draft,
        checks=base.checks,
    )


def planted_items(bases: list[BaseReply], per_base: int = 2, seed: int = 42) -> list[EvalItem]:
    """`per_base` planted errors per reply, balanced so every error type is used about equally:
    each reply takes the applicable types used least so far (ties broken by a seeded draw)."""
    by_id = {b.scenario_id: b for b in bases}
    suite = plant([sample_for(b) for b in bases], ERROR_TYPES, seed=seed)
    options: dict[str, dict[str, Any]] = {}
    for p in suite.planted:
        options.setdefault(p.sample_id, {})[p.error_type] = p
    used: Counter[str] = Counter()
    rng = random.Random(seed)
    items: list[EvalItem] = []
    for b in bases:
        avail = list(options.get(b.scenario_id, {}).items())
        rng.shuffle(avail)
        avail.sort(key=lambda kv: used[kv[0]])
        for name, p in avail[:per_base]:
            used[name] += 1
            reply = p.sample.fields["reply"]
            items.append(
                EvalItem(
                    item_id=p.case_id,
                    base_id=b.scenario_id,
                    kind="planted",
                    tier=b.gold_tier,
                    reply=reply,
                    error_type=name,
                    expected_dims=sorted(p.expected_items),
                    description=p.description,
                    checks=rerun_checks(by_id[b.scenario_id], reply),
                )
            )
    return items


FILLER = (
    "We understand that travel plans matter and we always aim to keep you informed.",
    "Our team works around the clock to make every journey as smooth as possible.",
    "Thank you for taking the time to write to us and for choosing to fly with us.",
)
WARMTH = (
    "We truly value you as our passenger and we are so grateful for your patience.",
    "Please know that your comfort and peace of mind are our highest priority.",
)
ERRORS_FOR_POLITENESS = ("wrong_regime", "fake_extraordinary")


def bias_items(bases: list[BaseReply], seed: int = 42) -> list[EvalItem]:
    """Verbosity padding (no defect) and politeness padding that hides one planted error."""
    rng = random.Random(seed)
    types = {e.name: e for e in ERROR_TYPES}
    items: list[EvalItem] = []
    for b in bases:
        pad = " ".join(rng.sample(FILLER, 2))
        items.append(
            EvalItem(
                item_id=f"{b.scenario_id}::verbosity",
                base_id=b.scenario_id,
                kind="verbosity",
                tier=b.gold_tier,
                reply=f"{b.draft.rstrip()}\n\n{pad}",
                checks=rerun_checks(b, f"{b.draft.rstrip()}\n\n{pad}"),
            )
        )
        name = ERRORS_FOR_POLITENESS[len(items) % 2]
        mutation = types[name].mutate(sample_for(b), random.Random(f"{seed}:{b.scenario_id}"))
        if mutation is None:
            continue
        warm = " ".join(WARMTH)
        reply = f"{warm}\n\n{mutation.changes['reply'].strip()}\n\n{rng.choice(FILLER)}"
        items.append(
            EvalItem(
                item_id=f"{b.scenario_id}::politeness_error",
                base_id=b.scenario_id,
                kind="politeness_error",
                tier=b.gold_tier,
                reply=reply,
                error_type=name,
                expected_dims=sorted(types[name].expected_items),
                description=f"politeness padding + {mutation.description}",
                checks=rerun_checks(b, reply),
            )
        )
    return items


def check_results(item: EvalItem) -> list[CheckResult]:
    return [CheckResult.model_validate(c) for c in item.checks]
