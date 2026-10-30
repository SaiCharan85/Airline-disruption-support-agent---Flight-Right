"""Build the evaluation report from the run records (docs/eval_protocol.md §7).

uv run python scripts/eval_report.py                  # final run -> eval/report.md
uv run python scripts/eval_report.py --pilot          # dev pilot -> data/eval/pilot/report.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flightright.agent.live import live_engine
from flightright.config import ROOT, load_config
from flightright.eval import metrics as m
from flightright.eval.public import pairwise_summary, rating_summary
from flightright.eval.runner import read_jsonl
from flightright.judge.judge import PROMPT_VERSION, load_airline_rubric

TIER_ORDER = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f} %"


def ci(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (n = 0)"
    lo, hi = r["ci95"]
    return f"{pct(r['rate'])} ({r['k']}/{r['n']}; 95 % CI {pct(lo)}-{pct(hi)})"


def num(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def git_commit() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            return (ROOT / ".git" / ref[5:]).read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


def config_hash() -> str:
    h = hashlib.sha256()
    for p in (
        ROOT / "config" / "app_config.yaml",
        ROOT / "config" / "rubrics" / "airline_v1.yaml",
        ROOT / "config" / "triage.yaml",
    ):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def primary_models() -> dict[str, str]:
    roles = load_config().models.roles
    return {r: roles[r].chain[0] for r in ("judge_a", "judge_b", "agent", "triage")}


def models_used(*groups: list[dict[str, Any]]) -> Counter[str]:
    c: Counter[str] = Counter()
    for rows in groups:
        for r in rows:
            c.update((r.get("models") or {}).keys())
    return c


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(r) + " |" for r in rows),
    ]


def triage_section(path: Path) -> list[str]:
    rows = read_jsonl(path)
    if not rows:
        return ["Not run yet (`scripts/eval_triage.py --split test --final`)."]
    severe = [r for r in rows if r["gold"] in ("S3", "S4")]
    hit = sum(1 for r in severe if TIER_ORDER[r["merged"]] >= 3)
    under = sum(1 for r in rows if TIER_ORDER[r["merged"]] < TIER_ORDER[r["gold"]])
    over = sum(1 for r in rows if TIER_ORDER[r["merged"]] > TIER_ORDER[r["gold"]])
    exact = sum(1 for r in rows if r["merged"] == r["gold"])
    return table(
        ["Scenarios", "Exact tier", "Recall@S3+", "Under-triage", "Over-triage"],
        [
            [
                str(len(rows)),
                ci(m.rate(exact, len(rows))),
                ci(m.rate(hit, len(severe))),
                pct(under / len(rows)),
                pct(over / len(rows)),
            ]
        ],
    )


def simulator_section(path: Path) -> list[str]:
    rows = read_jsonl(path)
    if not rows:
        return ["Not run yet (`scripts/simulate.py --split test --final --n 6`)."]
    ended = Counter(r["ended_by"] for r in rows)
    resolved = sum(1 for r in rows if r["ended_by"] == "passenger_done" or r.get("resolution"))
    turns = [len(r["turns"]) for r in rows]
    return table(
        [
            "Conversations",
            "Resolved (by the agent, or by a human after handoff)",
            "Mean turns",
            "Handoff rate",
            "Ended by",
        ],
        [
            [
                str(len(rows)),
                ci(m.rate(resolved, len(rows))),
                num(statistics.fmean(turns), 1),
                pct(sum(r["handed_off"] for r in rows) / len(rows)),
                ", ".join(f"{k} {v}" for k, v in ended.most_common()),
            ]
        ],
    )


def family_swap(aware: list[dict[str, Any]]) -> list[str]:
    """judge_a vs judge_b on the same S3 items (from the panel's member scores)."""
    diffs: dict[str, list[int]] = defaultdict(list)
    agree, n = 0, 0
    for r in aware:
        panel = r.get("panel") or {}
        members = panel.get("member_scores") or []
        if len(members) != 2:
            continue
        n += 1
        passed = panel.get("member_passed") or []
        agree += len(set(passed)) == 1
        a = {s["id"]: s for s in members[0]}
        for s in members[1]:
            if s["kind"] == "scale" and s.get("score") is not None:
                other = a.get(s["id"], {}).get("score")
                if other is not None:
                    diffs[s["id"]].append(other - s["score"])
    if not n:
        return ["No S3 panel records."]
    lines = [
        f"S3 items judged by both panel members: {n}; same pass/fail: {ci(m.rate(agree, n))}.",
        "",
    ]
    lines += table(
        ["Dimension", "Mean judge_a - judge_b", "n"],
        [[d, num(statistics.fmean(v)), str(len(v))] for d, v in sorted(diffs.items())],
    )
    return lines


def publish_signals(
    pilot: bool,
    rule: dict[str, Any],
    leak_a: dict[str, Any],
    leak_f: dict[str, Any],
    over_a: dict[str, Any],
    over_f: dict[str, Any],
    catch_a: dict[str, Any],
    catch_f: dict[str, Any],
    pairs_a: dict[str, Any],
    eng_fair: dict[str, Any] | None,
    fair: list[dict[str, Any]],
    fair_bases: dict[str, Any],
) -> None:
    """Summaries the ops API serves to the Judge Monitor and metrics screens (FR-17.3)."""
    if pilot:
        return  # pilot numbers are not results
    d = ROOT / "data" / "eval"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    planted = {
        "run": "final (test split)",
        "generated": today,
        "catch_rate": catch_a["overall"]["rate"],
        "pair_accuracy": pairs_a["overall"]["rate"],
        "severe_leak_rate": leak_a["rate"],
        "s1_overblocking": over_a["rate"],
        "decision_rule": rule["outcome"],
        "baseline": {
            "severe_leak_rate": leak_f["rate"],
            "s1_overblocking": over_f["rate"],
            "catch_rate": catch_f["overall"]["rate"],
        },
        "by_dimension": {k: v["rate"] for k, v in catch_a["dimension"].items()},
    }
    (d / "planted_errors.json").write_text(json.dumps(planted, indent=2), encoding="utf-8")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in fair:
        g = (fair_bases.get(r["base_id"], {}).get("base") or {}).get("counterfactual_group")
        groups[g or "?"].append(r)
    gaps = (
        m.fairness_gaps(groups)
        if groups
        else {"max_score_gap": None, "groups_with_approval_gap": 0}
    )
    fairness = {
        "generated": today,
        "engine_groups": len((eng_fair or {}).get("groups", {})),
        "engine_groups_with_differences": (eng_fair or {}).get("groups_with_differences"),
        "judged_groups": len(groups),
        "max_score_gap": gaps["max_score_gap"],
        "groups_with_approval_gap": gaps["groups_with_approval_gap"],
    }
    (d / "fairness.json").write_text(json.dumps(fairness, indent=2), encoding="utf-8")


def rejudged_section(
    d: Path, aware: list[dict[str, Any]], fixed: list[dict[str, Any]]
) -> list[str]:
    """Protocol change 1: the headline with infrastructure failures judged once more."""
    ra = {r["item_id"]: r for r in first(d / "judged_aware_rejudged.jsonl")}
    rf = {r["item_id"]: r for r in first(d / "judged_fixed_rejudged.jsonl")}
    if not ra and not rf:
        return []
    a2 = [ra.get(r["item_id"], r) for r in aware]
    f2 = [rf.get(r["item_id"], r) for r in fixed]
    leak_a, leak_f = m.severe_leak(a2), m.severe_leak(f2)
    over_a, over_f = m.s1_overblocking(a2), m.s1_overblocking(f2)
    req_a = statistics.fmean(r["requests"] for r in a2)
    req_f = statistics.fmean(r["requests"] for r in f2)
    rule = m.decision_rule(
        {"leak": leak_a, "overblock": over_a, "requests": req_a},
        {"leak": leak_f, "overblock": over_f, "requests": req_f},
    )
    by_f = {r["item_id"]: r for r in f2}
    s3 = [r for r in a2 if r["tier"] == "S3" and r["kind"] == "planted"]
    mc = m.mcnemar_exact(
        [not m.approved(r) for r in s3], [not m.approved(by_f[r["item_id"]]) for r in s3]
    )
    ua = m.rate(sum(1 for r in a2 if r.get("error")), len(a2))
    uf = m.rate(sum(1 for r in f2 if r.get("error")), len(f2))
    ca, cf = m.catch_rates(a2), m.catch_rates(f2)
    pa, pf = m.pair_accuracy(a2), m.pair_accuracy(f2)
    return [
        "### 1b. After re-judging infrastructure failures (protocol change 1)",
        "",
        f"Re-judged once: {len(ra)} severity-aware and {len(rf)} baseline items whose verdict failed "
        "on rate limits, provider 5xx errors or timeouts. Content failures are kept. The table "
        "above stays the registered primary result.",
        "",
        *table(
            ["Metric", "Severity-aware (ours)", "Fixed-strictness baseline"],
            [
                ["Severe leak rate", ci(leak_a), ci(leak_f)],
                ["S1 over-blocking", ci(over_a), ci(over_f)],
                ["Catch rate (all planted errors)", ci(ca["overall"]), ci(cf["overall"])],
                ["Controlled-pair accuracy", ci(pa["overall"]), ci(pf["overall"])],
                ["Model requests per item (mean)", num(req_a), num(req_f)],
                ["No trustworthy verdict", ci(ua), ci(uf)],
            ],
        ),
        "",
        f"Decision rule on these numbers: **{rule['outcome']}** (leak lower by >= 5 points: "
        f"{'yes' if rule['leak_lower_by_5pp'] else 'no'}; over-blocking within 5 points: "
        f"{'yes' if rule['overblock_within_5pp'] else 'no'}; requests within 2x: "
        f"{'yes' if rule['requests_within_2x'] else 'no'}). McNemar on the {len(s3)} S3 bad items: "
        f"only ours {mc['b']}, only the baseline {mc['c']}, p = {num(mc['p'], 3)}.",
        "",
    ]


def failure_cause(error: str) -> str:
    if "evidence not found" in error:
        return (
            "evidence: urgency or fairness check"
            if re.search(r"'(urgency|fairness_safety)'", error)
            else "evidence: other dimension"
        )
    if re.search(r"rate limited|429|HTTP 5\d\d|timed out|ReadTimeout", error):
        return "infrastructure (rate limit, 5xx, timeout)"
    if "invalid output" in error:
        return "invalid output"
    return "other"


def exploratory_section(
    d: Path, aware: list[dict[str, Any]], fixed: list[dict[str, Any]]
) -> list[str]:
    """Not pre-registered: where the unavailable verdicts come from, after change 1."""
    ra = {r["item_id"]: r for r in first(d / "judged_aware_rejudged.jsonl")}
    rf = {r["item_id"]: r for r in first(d / "judged_fixed_rejudged.jsonl")}
    a2 = [ra.get(r["item_id"], r) for r in aware]
    f2 = [rf.get(r["item_id"], r) for r in fixed]
    causes_a = Counter(failure_cause(r["error"]) for r in a2 if r.get("error"))
    causes_f = Counter(failure_cause(r["error"]) for r in f2 if r.get("error"))
    s1 = [r for r in a2 if r["tier"] == "S1" and r["kind"] == "original" and not m.approved(r)]
    s1_judge = sum(1 for r in s1 if not r.get("error"))
    both = {r["item_id"] for r in a2 if not r.get("error")} & {
        r["item_id"] for r in f2 if not r.get("error")
    }
    a3 = [r for r in a2 if r["item_id"] in both]
    f3 = [r for r in f2 if r["item_id"] in both]
    keys = sorted(set(causes_a) | set(causes_f))
    return [
        "### 1c. Exploratory (not pre-registered): where the unavailable verdicts come from",
        "",
        *table(
            ["Cause (after change 1)", "Severity-aware", "Baseline"],
            [[k, str(causes_a.get(k, 0)), str(causes_f.get(k, 0))] for k in keys],
        ),
        "",
        f"Of the {len(s1)} good S1 replies the severity-aware judge did not approve, {s1_judge} "
        f"{'was' if s1_judge == 1 else 'were'} judged below the bar and {len(s1) - s1_judge} "
        f"had no trustworthy verdict.",
        "",
        "Most evidence failures are on the must-pass **urgency** and **fairness/safety** checks, "
        "and they hit the severity-aware judge far more often. It reads the triage context. When "
        "that context says urgency is 'none', the check passes trivially, but the rubric still "
        "demands a verbatim quote from the draft, and there is nothing in the draft to quote. The "
        "baseline does not see the triage context and rarely hits this. Letting a must-pass check "
        "that passes trivially skip the quote is the clearest next change. It would need a new "
        "evaluation run.",
        "",
        f"On the {len(both)} items where both systems produced a verdict: severe leak rate ours "
        f"{ci(m.severe_leak(a3))}, baseline {ci(m.severe_leak(f3))}; S1 over-blocking ours "
        f"{ci(m.s1_overblocking(a3))}, baseline {ci(m.s1_overblocking(f3))}.",
        "",
    ]


def first(path: Path, key: str = "item_id") -> list[dict[str, Any]]:
    """One record per item: the first written (a resumed run never re-judges an item, but two
    overlapping runs can both write one)."""
    seen: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(path):
        seen.setdefault(r[key], r)
    return list(seen.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    d = ROOT / "data" / "eval" / ("pilot" if args.pilot else "final")
    out = d / "report.md" if args.pilot else ROOT / "eval" / "report.md"
    # Only records of the current item set count (an interrupted run may leave older items behind).
    current = {r["item_id"] for r in read_jsonl(d / "items.jsonl")}
    aware = [r for r in first(d / "judged_aware.jsonl") if r["item_id"] in current]
    fixed = [r for r in first(d / "judged_fixed.jsonl") if r["item_id"] in current]
    if not aware or not fixed:
        print(f"no headline records in {d}", file=sys.stderr)
        return 1
    bases = read_jsonl(d / "bases.jsonl")
    only = first(d / "judged_judge_only.jsonl")
    bias = first(d / "judged_bias.jsonl")
    hs3 = [r for r in read_jsonl(d / "public_hs3.jsonl") if "error" not in r]
    hs2 = [r for r in read_jsonl(d / "public_hs2.jsonl") if "error" not in r]
    fair = first(d / "judged_fairness.jsonl")
    fair_bases = {r["scenario_id"]: r for r in read_jsonl(d / "fairness_bases.jsonl")}
    eng_fair_path = d / "engine_fairness.json"
    eng_fair = (
        json.loads(eng_fair_path.read_text(encoding="utf-8")) if eng_fair_path.is_file() else None
    )
    split = "dev (pilot, not a result)" if args.pilot else "test (held out, final run)"
    manifest = d / "manifest.json"
    frozen = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
    unavail_a = m.rate(sum(1 for r in aware if r.get("error")), len(aware))
    unavail_f = m.rate(sum(1 for r in fixed if r.get("error")), len(fixed))

    leak_a, leak_f = m.severe_leak(aware), m.severe_leak(fixed)
    over_a, over_f = m.s1_overblocking(aware), m.s1_overblocking(fixed)
    req_a = statistics.fmean(r["requests"] for r in aware)
    req_f = statistics.fmean(r["requests"] for r in fixed)
    rule = m.decision_rule(
        {"leak": leak_a, "overblock": over_a, "requests": req_a},
        {"leak": leak_f, "overblock": over_f, "requests": req_f},
    )
    by_id_f = {r["item_id"]: r for r in fixed}
    s3_bad = [
        r for r in aware if r["tier"] == "S3" and r["kind"] == "planted" and r["item_id"] in by_id_f
    ]
    mc = m.mcnemar_exact(
        [not m.approved(r) for r in s3_bad], [not m.approved(by_id_f[r["item_id"]]) for r in s3_bad]
    )
    catch_a, catch_f = m.catch_rates(aware), m.catch_rates(fixed)
    pairs_a, pairs_f = m.pair_accuracy(aware), m.pair_accuracy(fixed)
    lat_a, lat_f = m.requests_and_latency(aware), m.requests_and_latency(fixed)
    used = models_used(aware, fixed, only, bias, fair)
    primary = primary_models()
    fallback = sum(
        1
        for r in aware + fixed
        if any(k not in (primary["judge_a"], primary["judge_b"]) for k in (r.get("models") or {}))
    )
    ruleset_versions = {k: v["version"] for k, v in live_engine().rulesets.items()}
    rubric = load_airline_rubric()
    dropped = Counter(r["status"] for r in bases if r["base"] is None)
    usable = Counter(r["base"]["gold_tier"] for r in bases if r["base"])

    lines: list[str] = []
    lines += [
        "# Evaluation report",
        "",
        f"Split: **{split}**. Protocol: [docs/eval_protocol.md](../docs/eval_protocol.md). "
        "Every number below is computed by `scripts/eval_report.py` from the run records.",
        "",
        "## Run manifest",
        "",
        *(
            [
                "Protocol frozen before the first final-run model call: `docs/eval_protocol.md` "
                f"sha256 `{frozen['protocol_sha256'][:16]}...` at {frozen['frozen_at']}. "
                + (
                    "The file is unchanged since."
                    if hashlib.sha256((ROOT / "docs" / "eval_protocol.md").read_bytes()).hexdigest()
                    == frozen["protocol_sha256"]
                    else "Since then only the 'Changes after registration' section was added "
                    "(change 1, applied in section 1b)."
                ),
                "",
            ]
            if frozen
            else []
        ),
        *table(
            ["Item", "Value"],
            [
                ["Generated", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")],
                ["Git commit", f"`{git_commit()}`"],
                ["Config hash (app_config, rubric, triage)", f"`{config_hash()}`"],
                ["Rubric", f"`{rubric.name}@{rubric.version}`, prompt `{PROMPT_VERSION}`"],
                ["Exemplar set", "none (no reviewer exemplars in evaluation runs)"],
                ["Rulesets", ", ".join(f"`{k}` {v}" for k, v in sorted(ruleset_versions.items()))],
                ["Primary models", ", ".join(f"{k}: `{v}`" for k, v in primary.items())],
                [
                    "Judge models actually called",
                    ", ".join(f"`{k}` ({v} items)" for k, v in used.most_common()),
                ],
                ["Items with a fallback judge model", f"{fallback} of {len(aware) + len(fixed)}"],
                ["Cache", "bypassed for model calls" if not args.pilot else "used (pilot)"],
                ["Cost", "$0 (free tiers only)"],
            ],
        ),
        "",
        "## Base replies",
        "",
        f"Drafted {len(bases)} scenarios; usable {sum(usable.values())} "
        f"({', '.join(f'{t} {n}' for t, n in sorted(usable.items()))}). "
        f"Not usable: {', '.join(f'{k} {v}' for k, v in dropped.most_common()) or 'none'} "
        "(`handed_off` covers cases the engine sends to review under unverified rules).",
        "",
        f"Evaluation items: {len(aware)} "
        f"({sum(r['kind'] == 'original' for r in aware)} presumed-good originals, "
        f"{sum(r['kind'] == 'planted' for r in aware)} with one planted error each).",
        "",
        "## 1. Headline: severity-aware vs fixed-strictness judge",
        "",
        *table(
            ["Metric", "Severity-aware (ours)", "Fixed-strictness baseline", "Target"],
            [
                ["Severe leak rate (S3 bad replies approved)", ci(leak_a), ci(leak_f), "≤ 1 %"],
                [
                    "S1 over-blocking (good S1 replies not approved)",
                    ci(over_a),
                    ci(over_f),
                    "≤ 10 %",
                ],
                ["Model requests per item (mean)", num(req_a), num(req_f), "—"],
                [
                    "No trustworthy verdict (judge unavailable; handed off)",
                    ci(unavail_a),
                    ci(unavail_f),
                    "—",
                ],
                [
                    "Judge latency p95 (ms)",
                    num(lat_a["all"]["latency_ms_p95"], 0),
                    num(lat_f["all"]["latency_ms_p95"], 0),
                    "—",
                ],
            ],
        ),
        "",
        f"**Decision rule (pre-registered): {rule['outcome']}.** "
        f"Leak lower by ≥ 5 points (or both ≤ 1 %): {'yes' if rule['leak_lower_by_5pp'] else 'no'}; "
        f"over-blocking within 5 points of the baseline: {'yes' if rule['overblock_within_5pp'] else 'no'}; "
        f"requests within 2x: {'yes' if rule['requests_within_2x'] else 'no'}.",
        "",
        f"Paired exact McNemar test on the {len(s3_bad)} S3 bad items (caught or not): "
        f"caught only by ours {mc['b']}, only by the baseline {mc['c']}, p = {num(mc['p'], 3)}.",
        "",
        *rejudged_section(d, aware, fixed),
        *exploratory_section(d, aware, fixed),
        "## 2. Catch rate (planted errors not approved)",
        "",
        f"Overall: ours {ci(catch_a['overall'])}, baseline {ci(catch_f['overall'])}. "
        f"Caught by code checks before any model call: {catch_a['overall']['by_code']}.",
        "",
        *table(
            ["Error type", "Ours", "Baseline", "Caught by code"],
            [
                [k, ci(v), ci(catch_f["error_type"].get(k, m.rate(0, 0))), str(v["by_code"])]
                for k, v in catch_a["error_type"].items()
            ],
        ),
        "",
        *table(
            ["Dimension", "Ours", "Baseline"],
            [
                [k, ci(v), ci(catch_f["dimension"].get(k, m.rate(0, 0)))]
                for k, v in catch_a["dimension"].items()
            ],
        ),
        "",
        *table(
            ["Tier", "Ours", "Baseline"],
            [
                [k, ci(v), ci(catch_f["tier"].get(k, m.rate(0, 0)))]
                for k, v in catch_a["tier"].items()
            ],
        ),
        "",
    ]
    if only:
        oc = m.catch_rates(only)
        lines += [
            "**Judge on its own** (correctness errors on S2 items, judge told the code checks "
            f"passed): {ci(oc['overall'])}.",
            "",
            *table(
                ["Error type", "Judge-only catch"],
                [[k, ci(v)] for k, v in oc["error_type"].items()],
            ),
            "",
        ]
    lines += [
        "## 3. Controlled pairs (original beats its planted twin on an affected dimension)",
        "",
        f"Ours {ci(pairs_a['overall'])}; baseline {ci(pairs_f['overall'])}. "
        f"Pairs skipped because a code check caught the twin before any model call: "
        f"{pairs_a['skipped_code_caught']}. Target for subjective dimensions: ≥ 85 %.",
        "",
        *table(
            ["Error type", "Ours", "Baseline"],
            [
                [k, ci(v), ci(pairs_f["error_type"].get(k, m.rate(0, 0)))]
                for k, v in pairs_a["error_type"].items()
            ],
        ),
        "",
        "## 4. Requests and latency per tier",
        "",
        *table(
            [
                "Tier",
                "Ours: requests mean / p95",
                "Ours: latency p95 ms",
                "Baseline: requests mean / p95",
            ],
            [
                [
                    t,
                    f"{num(v['requests_mean'])} / {num(v['requests_p95'], 0)}",
                    num(v["latency_ms_p95"], 0),
                    f"{num(lat_f[t]['requests_mean'])} / {num(lat_f[t]['requests_p95'], 0)}"
                    if t in lat_f
                    else "n/a",
                ]
                for t, v in lat_a.items()
            ],
        ),
        "",
        "## 5. Judge bias",
        "",
    ]
    if bias:
        originals = [r for r in aware if r["kind"] == "original"]
        verb = [r for r in bias if r["kind"] == "verbosity"]
        polite = [r for r in bias if r["kind"] == "politeness_error"]
        shift = m.score_shift(originals, verb)
        lines += [
            "Verbosity padding (two neutral filler sentences, no defect): mean score shift.",
            "",
            *table(
                ["Dimension", "Mean shift (padded - original)", "n"],
                [[k, num(v["mean_shift"]), str(v["n"])] for k, v in shift.items()],
            ),
            "",
            f"Verbosity-padded replies still approved: {ci(m.rate(sum(m.approved(r) for r in verb), len(verb)))}.",
            "",
            "Politeness padding hiding one planted error (wrong regime or invented extraordinary "
            f"circumstances): still approved {ci(m.rate(sum(m.approved(r) for r in polite), len(polite)))}.",
            "",
        ]
    else:
        lines += ["Not run.", ""]
    lines += [
        "Model-family swap (judge_a vs judge_b on the same S3 items):",
        "",
        *family_swap(aware),
        "",
    ]
    lines += ["## 6. Public human judgments (general domain, not airline validation)", ""]
    if hs3 or hs2:
        if hs3:
            s = pairwise_summary(hs3)
            lines += [
                f"HelpSteer3 preference (validation), judge_a, both orders: accuracy "
                f"{ci(s['accuracy_both_orders'])}; position consistency {ci(s['position_consistency'])}; "
                f"accuracy on strong preferences (|strength| ≥ 2) {ci(s['accuracy_strong_preferences'])}.",
                "",
            ]
        if hs2:
            rs = rating_summary(hs2)
            lines += table(
                ["HelpSteer2 attribute", "Spearman (judge vs human)", "n"],
                [[a, num(v["spearman"]), str(v["n"])] for a, v in rs.items()],
            )
            lines += [""]
    else:
        lines += ["Not run.", ""]
    lines += ["## 7. Fairness (counterfactual passengers)", ""]
    if eng_fair:
        lines += [
            f"Entitlements across all {len(eng_fair['groups'])} counterfactual groups: groups with "
            f"any difference **{eng_fair['groups_with_differences']}** (target 0).",
            "",
        ]
    if fair:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in fair:
            g = (fair_bases.get(r["base_id"], {}).get("base") or {}).get("counterfactual_group")
            groups[g or "?"].append(r)
        gaps = m.fairness_gaps(groups)
        not_drafted = sum(1 for r in fair_bases.values() if r["base"] is None)
        lines += [
            f"Judged variants: {len(fair)} in {len(groups)} groups ({not_drafted} variants not "
            "usable as replies). Largest within-group gap in mean scale score: "
            f"{num(gaps['max_score_gap'])}; groups where one variant was approved and another not: "
            f"{gaps['groups_with_approval_gap']}.",
            "",
            *table(
                ["Group", "Variants", "Score gap", "Approval differs"],
                [
                    [
                        g,
                        str(v["variants"]),
                        num(v["score_gap"]),
                        "yes" if v["approval_gap"] else "no",
                    ]
                    for g, v in gaps["groups"].items()
                ],
            ),
            "",
        ]
    split_name = "dev" if args.pilot else "test"
    lines += [
        "## 8. Triage (held out)",
        "",
        *triage_section(ROOT / "data" / "eval" / f"triage_{split_name}.jsonl"),
        "",
        "## 9. Simulator",
        "",
        *simulator_section(ROOT / "data" / "eval" / f"simulations_{split_name}.jsonl"),
        "",
        "## Limitations",
        "",
        "- No large-scale human labels for airline replies: ground truth is planted errors (bad by "
        "construction) and presumed-good drafts (not human-checked), so S1 over-blocking is an upper "
        "bound.",
        "- Planted errors are rule-based edits, cleaner than real model mistakes; catch rates are "
        "likely optimistic.",
        "- The public human sets are general-domain; they test the judge model's general calibration, "
        "not its airline-domain validity.",
        "- Scenarios and passenger messages are synthetic.",
        "- Free-tier sample sizes are small; the confidence intervals above are wide, and only large "
        "differences are detectable.",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    publish_signals(
        args.pilot,
        rule,
        leak_a,
        leak_f,
        over_a,
        over_f,
        catch_a,
        catch_f,
        pairs_a,
        eng_fair,
        fair,
        fair_bases,
    )
    print(f"wrote {out.relative_to(ROOT)}  (decision rule: {rule['outcome']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
