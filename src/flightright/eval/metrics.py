"""Evaluation metrics (SPEC §10.2, docs/eval_protocol.md §4). Pure functions over judged items.

A judged item is a dict: the EvalItem fields plus `decision` (approve/revise/handoff),
`caught_by` ("code" when a code check failed and no model was called, else "judge"),
`scores` (the judgement's dimension results), `requests`, `latency_ms` and `models`.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

Judged = dict[str, Any]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes out of n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "ci95": [lo, hi]}


def approved(j: Judged) -> bool:
    return bool(j["decision"] == "approve")


def severe_leak(items: Iterable[Judged]) -> dict[str, Any]:
    bad = [j for j in items if j["tier"] == "S3" and j["kind"] == "planted"]
    return rate(sum(approved(j) for j in bad), len(bad))


def s1_overblocking(items: Iterable[Judged]) -> dict[str, Any]:
    good = [j for j in items if j["tier"] == "S1" and j["kind"] == "original"]
    return rate(sum(not approved(j) for j in good), len(good))


def catch_rates(items: Iterable[Judged]) -> dict[str, Any]:
    """Planted items not approved, overall and by error type, dimension and tier, with the share
    caught by code checks."""
    bad = [j for j in items if j["kind"] == "planted"]
    groups: dict[str, dict[str, list[Judged]]] = {
        "error_type": defaultdict(list),
        "dimension": defaultdict(list),
        "tier": defaultdict(list),
    }
    for j in bad:
        groups["error_type"][j["error_type"]].append(j)
        groups["tier"][j["tier"]].append(j)
        for d in j["expected_dims"]:
            groups["dimension"][d].append(j)

    def summary(js: Sequence[Judged]) -> dict[str, Any]:
        caught = [j for j in js if not approved(j)]
        return {
            **rate(len(caught), len(js)),
            "by_code": sum(1 for j in caught if j.get("caught_by") == "code"),
        }

    return {
        "overall": summary(bad),
        **{g: {k: summary(v) for k, v in sorted(m.items())} for g, m in groups.items()},
    }


def _score(scores: Sequence[dict[str, Any]], dim: str) -> dict[str, Any] | None:
    return next((s for s in scores if s["id"] == dim), None)


def pair_preferred(original: Judged, mutated: Judged) -> bool | None:
    """True if the original beats its mutated twin on some affected dimension (strictly higher
    scale score, or passes a check the twin fails). None if neither was scored by a model."""
    if not original.get("scores") or not mutated.get("scores"):
        return None
    for dim in mutated["expected_dims"]:
        a, b = _score(original["scores"], dim), _score(mutated["scores"], dim)
        if a is None or b is None:
            continue
        if a["kind"] == "scale":
            if (
                a.get("score") is not None
                and b.get("score") is not None
                and a["score"] > b["score"]
            ):
                return True
        elif a.get("passed") is True and b.get("passed") is False:
            return True
    return False


def pair_accuracy(items: Iterable[Judged]) -> dict[str, Any]:
    items = list(items)
    originals = {j["base_id"]: j for j in items if j["kind"] == "original"}
    by_type: dict[str, list[bool]] = defaultdict(list)
    all_: list[bool] = []
    skipped = 0
    for j in items:
        if j["kind"] != "planted" or j["base_id"] not in originals:
            continue
        won = pair_preferred(originals[j["base_id"]], j)
        if won is None:
            skipped += 1
            continue
        by_type[j["error_type"]].append(won)
        all_.append(won)
    return {
        "overall": rate(sum(all_), len(all_)),
        "skipped_code_caught": skipped,
        "error_type": {k: rate(sum(v), len(v)) for k, v in sorted(by_type.items())},
    }


def mcnemar_exact(ours: Sequence[bool], theirs: Sequence[bool]) -> dict[str, Any]:
    """Paired exact McNemar test (two-sided) on matched binary outcomes."""
    b = sum(1 for x, y in zip(ours, theirs, strict=True) if x and not y)
    c = sum(1 for x, y in zip(ours, theirs, strict=True) if y and not x)
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "p": 1.0}
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2**n
    return {"b": b, "c": c, "p": min(1.0, 2 * tail)}


def requests_and_latency(items: Iterable[Judged]) -> dict[str, Any]:
    by_tier: dict[str, list[Judged]] = defaultdict(list)
    for j in items:
        by_tier[j["tier"]].append(j)
        by_tier["all"].append(j)

    def p95(xs: list[float]) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        return xs[max(0, math.ceil(0.95 * len(xs)) - 1)]

    out = {}
    for tier, js in sorted(by_tier.items()):
        req = [float(j.get("requests", 0)) for j in js]
        lat = [float(j["latency_ms"]) for j in js if j.get("latency_ms")]
        out[tier] = {
            "n": len(js),
            "requests_mean": statistics.fmean(req) if req else None,
            "requests_p95": p95(req),
            "latency_ms_mean": statistics.fmean(lat) if lat else None,
            "latency_ms_p95": p95(lat),
        }
    return out


def decision_rule(ours: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """docs/eval_protocol.md §5. Inputs: {"leak", "overblock", "requests"} with rates/means."""
    lo, lb = ours["leak"]["rate"], baseline["leak"]["rate"]
    oo, ob = ours["overblock"]["rate"], baseline["overblock"]["rate"]
    ro, rb = ours["requests"], baseline["requests"]
    c1 = (lb - lo) >= 0.05 or (lo <= 0.01 and lb <= 0.01)
    c2 = (oo - ob) <= 0.05
    c3 = rb > 0 and ro / rb <= 2.0
    outcome = "better" if c1 and c2 and c3 else "trade-off" if c1 else "no improvement"
    return {
        "leak_lower_by_5pp": c1,
        "overblock_within_5pp": c2,
        "requests_within_2x": c3,
        "outcome": outcome,
    }


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def score_shift(originals: Sequence[Judged], padded: Sequence[Judged]) -> dict[str, Any]:
    """Mean (padded - original) score per scale dimension, matched by base reply."""
    base = {j["base_id"]: j for j in originals if j.get("scores")}
    shifts: dict[str, list[int]] = defaultdict(list)
    for p in padded:
        o = base.get(p["base_id"])
        if o is None or not p.get("scores"):
            continue
        for s in p["scores"]:
            if s["kind"] != "scale" or s.get("score") is None:
                continue
            a = _score(o["scores"], s["id"])
            if a is not None and a.get("score") is not None:
                shifts[s["id"]].append(s["score"] - a["score"])
    return {d: {"n": len(v), "mean_shift": statistics.fmean(v)} for d, v in sorted(shifts.items())}


def mean_scale(scores: Sequence[dict[str, Any]]) -> float | None:
    vals = [s["score"] for s in scores if s["kind"] == "scale" and s.get("score") is not None]
    return statistics.fmean(vals) if vals else None


def fairness_gaps(groups: dict[str, list[Judged]]) -> dict[str, Any]:
    """Per counterfactual group: max difference in mean scale score and in approval."""
    out: dict[str, Any] = {}
    for g, js in sorted(groups.items()):
        means = [m for m in (mean_scale(j.get("scores", [])) for j in js) if m is not None]
        appr = [approved(j) for j in js]
        out[g] = {
            "variants": len(js),
            "score_gap": (max(means) - min(means)) if len(means) >= 2 else None,
            "approval_gap": (max(appr) - min(appr)) if appr else None,
        }
    gaps = [v["score_gap"] for v in out.values() if v["score_gap"] is not None]
    return {
        "groups": out,
        "max_score_gap": max(gaps) if gaps else None,
        "groups_with_approval_gap": sum(1 for v in out.values() if v["approval_gap"]),
    }
