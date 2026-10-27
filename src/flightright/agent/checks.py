"""Deterministic code checks on a draft reply (FR-7). The judge can never override a failure.

Claims are extracted from the draft with patterns (amounts, regime references, timelines, voucher
and care mentions) and compared with what the engine and the tools actually said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flightright.models import CheckResult, Entitlement

AMOUNT = re.compile(r"(?:€|eur(?:o|os)?\s?)\s?(\d[\d,.]*)|(\d[\d,.]*)\s?(?:€|eur(?:o|os)?\b)", re.I)
TIMELINE = re.compile(
    r"\bwithin\s+(\d+|seven|fourteen|thirty)\s+(calendar\s+|working\s+)?(days?|hours?)\b", re.I
)
WORD_NUMBERS = {"seven": 7, "fourteen": 14, "thirty": 30}
# Promises of a response time that no tool or rule backs ("within the next hour", "in 30 minutes").
RESPONSE_PROMISE = re.compile(
    r"\b(within|in)\s+(the\s+next\s+)?(an?\s+|\d+\s+|one\s+|two\s+|a\s+few\s+)?(hours?|minutes?|mins?)\b",
    re.I,
)
AMENDED_REF = re.compile(r"2026/2202|amended (eu261|regulation|rules)|new (eu261 )?rules", re.I)
OLD_ONLY_REF = re.compile(
    r"\b(7|14) days? to two weeks\b|between (seven|7) and (fourteen|14) days", re.I
)
TRAVEL_VOUCHER = re.compile(
    r"\b(travel voucher|flight voucher|future travel|travel credit|credit for a future"
    r"|voucher (for|towards) (a )?(future|next|another) (flight|trip|booking))\b",
    re.I,
)
CASH_RIGHT = re.compile(
    r"\b(cash|bank transfer|money back|refund (to|in) your|in money|monetary|"
    r"you can (choose|ask for) (a )?(refund|payment)|instead of (the )?voucher)\b",
    re.I,
)
CARE_MENTION = re.compile(
    r"\b(meal|food|refreshment|drink|snack|voucher for (food|meals?)|hotel|"
    r"accommodation|care)\b",
    re.I,
)
COMPENSATION_PROMISE = re.compile(
    r"\b(entitled to|you will (receive|get)|we will pay|eligible for)\b.{0,40}"
    r"\bcompensation\b",
    re.I,
)
REDUCTION_MENTION = re.compile(r"\b(reduc\w+|lower(ed)? (amount|compensation)|50 ?%|half)\b", re.I)
# "No reduction applies" / "will not be reduced" are correct statements, not invented caveats.
NEGATED_REDUCTION = re.compile(r"\b(no|not|never|without( any)?)\s+(\w+\s+){0,2}reduc\w+", re.I)


@dataclass
class CheckInputs:
    entitlement: Entitlement | None
    issued_voucher_amounts: set[int] = field(default_factory=set)
    refund_amount: int | None = None
    care_expected: bool = False  # passenger is waiting now and the engine lists care
    allowed_timeline_days: set[int] = field(default_factory=lambda: {7})


def _amounts(text: str) -> set[int]:
    out: set[int] = set()
    for a, b in AMOUNT.findall(text):
        raw = (a or b).replace(",", "").split(".")[0]
        if raw.isdigit():
            out.add(int(raw))
    return out


def run_checks(draft: str, inp: CheckInputs) -> list[CheckResult]:
    ent = inp.entitlement
    results: list[CheckResult] = []

    allowed = set(inp.issued_voucher_amounts)
    if inp.refund_amount:
        allowed.add(inp.refund_amount)
    if ent and ent.confidence == "verified":
        allowed |= {a for a in (ent.compensation_eur, ent.may_reduce_to_eur) if a}
    stray = sorted(_amounts(draft) - allowed)
    results.append(
        CheckResult(
            check_id="amounts_match_entitlement",
            passed=not stray,
            detail=f"amounts not backed by the engine or tools: {stray}" if stray else "",
        )
    )

    promised = bool(COMPENSATION_PROMISE.search(draft))
    owed = bool(ent and ent.compensation_eur and ent.confidence == "verified")
    results.append(
        CheckResult(
            check_id="no_unowed_compensation_promise",
            passed=not promised or owed,
            detail="promises compensation the engine does not grant"
            if promised and not owed
            else "",
        )
    )

    reduction_listed = bool(ent and ent.may_reduce_to_eur)
    mentions_reduction = bool(REDUCTION_MENTION.search(NEGATED_REDUCTION.sub(" ", draft)))
    results.append(
        CheckResult(
            check_id="no_unlisted_reduction",
            passed=reduction_listed or not mentions_reduction,
            detail="mentions a compensation reduction the engine does not list"
            if mentions_reduction and not reduction_listed
            else "",
        )
    )

    regime_ok = True
    if ent and ent.regime == "eu261_2004" and AMENDED_REF.search(draft):
        regime_ok = False
    if ent and ent.regime == "eu261_amended_2027" and OLD_ONLY_REF.search(draft):
        regime_ok = False
    results.append(
        CheckResult(
            check_id="regime_matches_flight_date",
            passed=regime_ok,
            detail=""
            if regime_ok
            else f"draft cites rules that do not apply ({ent and ent.regime})",
        )
    )

    voucher_ok = not TRAVEL_VOUCHER.search(draft) or bool(CASH_RIGHT.search(draft))
    results.append(
        CheckResult(
            check_id="voucher_with_cash_right",
            passed=voucher_ok,
            detail="" if voucher_ok else "travel voucher offered without the cash/refund right",
        )
    )

    care_ok = not inp.care_expected or bool(CARE_MENTION.search(draft))
    results.append(
        CheckResult(
            check_id="care_mentioned",
            passed=care_ok,
            detail="" if care_ok else "care (meals, refreshments, hotel) is owed but not mentioned",
        )
    )

    bad_timelines = []
    for num, _, _unit in TIMELINE.findall(draft):
        days = WORD_NUMBERS.get(num.lower()) or (int(num) if num.isdigit() else -1)
        if days not in inp.allowed_timeline_days:
            bad_timelines.append(days)
    promises = [m.group(0) for m in RESPONSE_PROMISE.finditer(draft)]
    results.append(
        CheckResult(
            check_id="timelines_supported",
            passed=not bad_timelines and not promises,
            detail="; ".join(
                filter(
                    None,
                    [
                        f"unsupported timelines (days): {bad_timelines}" if bad_timelines else "",
                        f"unbacked response-time promises: {promises}" if promises else "",
                    ],
                )
            ),
        )
    )
    return results


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed]
