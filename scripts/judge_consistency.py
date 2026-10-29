"""How consistent is the judge? Judge the same replies several times with the cache bypassed.

  uv run python scripts/judge_consistency.py --n 30 --repeats 3 --dry-run
  uv run python scripts/judge_consistency.py --n 30 --repeats 3     # -> docs/judge_consistency.md

Phase 1 drafts one reply per dev scenario through the real graph (placeholder judge, rules-only
triage; cached, so re-runs are free). Phase 2 judges every reply `--repeats` times with
`use_cache=False` and reports, per dimension, how often the score or pass/fail changes, and how
often the final decision flips. S4 scenarios are skipped (they go straight to a human).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from flightright.agent.judging import CodeChecksOnlyJudge
from flightright.agent.live import live_engine, load_scenarios, service_for
from flightright.config import ROOT, load_config, load_env
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient
from flightright.models import CheckResult, Entitlement, TriageResult
from flightright.triage.triage import Triage

OUT = ROOT / "data" / "eval" / "consistency_replies.jsonl"


def pick(n: int, seed: int) -> list[Any]:
    pool = [
        s
        for s in load_scenarios("dev")
        if s.gold_tier != "S4" and s.situation.timing == "at_airport"
    ]
    by_tier: dict[str, list[Any]] = defaultdict(list)
    for s in pool:
        by_tier[s.gold_tier].append(s)
    rng = random.Random(seed)
    chosen: list[Any] = []
    for tier in ("S1", "S2", "S3"):
        rng.shuffle(by_tier[tier])
    while len(chosen) < n and any(by_tier.values()):
        for tier in ("S3", "S2", "S1"):  # stratified, severe first
            if by_tier[tier] and len(chosen) < n:
                chosen.append(by_tier[tier].pop())
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    load_env()
    client = LLMClient.from_config(cfg.models)
    scenarios = pick(args.n, cfg.seed)
    tiers = Counter(s.gold_tier for s in scenarios)
    judge_calls = args.repeats * sum(2 if s.gold_tier == "S3" else 1 for s in scenarios)
    print(
        f"{len(scenarios)} dev scenarios {dict(tiers)}; phase 1 ~{len(scenarios)} draft requests "
        f"(cached after the first run); phase 2 ~{judge_calls} judge requests "
        "(+ evidence retries), cache bypassed"
    )
    if args.dry_run:
        return 0

    # ---------------------------------------------------------------- phase 1: replies
    engine = live_engine()
    done = (
        {
            json.loads(x)["scenario_id"]: json.loads(x)
            for x in OUT.read_text(encoding="utf-8").splitlines()
            if x
        }
        if OUT.is_file()
        else {}
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for s in scenarios:
            if s.scenario_id in done:
                continue
            service, cid = service_for(s, cfg, client, CodeChecksOnlyJudge(), engine, Triage(None))
            out = service.handle_message(cid, s.opening_message, "probe-1")
            st = service.state(cid)
            if out.status != "answered" or not st.get("attempts"):
                print(f"skip {s.scenario_id}: {out.status}")
                continue
            row = {
                "scenario_id": s.scenario_id,
                "gold_tier": s.gold_tier,
                "draft": out.reply_text,
                "transcript": st["transcript"][:-1],
                "triage": st["triage"],
                "entitlement": st["entitlement"],
                "checks": st["attempts"][-1]["code_checks"],
            }
            fh.write(json.dumps(row) + "\n")
            done[s.scenario_id] = row
    rows = [done[s.scenario_id] for s in scenarios if s.scenario_id in done]
    print(f"phase 1: {len(rows)} replies")

    # ---------------------------------------------------------------- phase 2: repeated judging
    judge = SeverityAwareJudge.for_client(client, use_cache=False)
    flips: dict[str, list[bool]] = defaultdict(list)
    spreads: dict[str, list[int]] = defaultdict(list)
    decision_flip: list[bool] = []
    by_tier_flip: dict[str, list[bool]] = defaultdict(list)
    unavailable = 0
    for i, row in enumerate(rows, 1):
        runs = []
        for _ in range(args.repeats):
            v = judge.evaluate(
                draft=row["draft"],
                transcript=row["transcript"],
                triage=TriageResult.model_validate(row["triage"]),
                entitlement=Entitlement.model_validate(row["entitlement"]),
                checks=[CheckResult.model_validate(c) for c in row["checks"]],
                target="reply",
            )
            runs.append(v.judgement)
        unavailable += sum(1 for r in runs if r.get("error"))
        ok = [r for r in runs if not r.get("error")]
        decisions = [r["decision"] for r in runs]
        decision_flip.append(len(set(decisions)) > 1)
        by_tier_flip[row["triage"]["tier"]].append(len(set(decisions)) > 1)
        if len(ok) >= 2:
            for dim in {x["id"] for x in ok[0]["scores"]}:
                vals = [next(x for x in r["scores"] if x["id"] == dim) for r in ok]
                key = "score" if vals[0]["kind"] == "scale" else "passed"
                seen = [x[key] for x in vals if x[key] is not None]
                flips[dim].append(len(set(seen)) > 1)
                if key == "score" and seen:
                    spreads[dim].append(max(seen) - min(seen))
        print(
            f"[{i}/{len(rows)}] {row['scenario_id']} {row['triage']['tier']}: decisions {decisions}"
        )

    lines = [
        "# Judge consistency",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d}: {len(rows)} dev replies, "
        f"each judged {args.repeats} times with "
        "the cache bypassed (temperature 0). A flip means the repeats did not all agree.",
        "",
        f"**Final decision flips:** {sum(decision_flip)}/{len(decision_flip)} replies "
        f"({sum(decision_flip) / max(1, len(decision_flip)):.0%}); "
        f"unavailable verdicts: {unavailable}.",
        "",
        "| tier | replies | decision flips |",
        "|---|---|---|",
    ]
    lines += [
        f"| {t} | {len(v)} | {sum(v)} ({sum(v) / len(v):.0%}) |"
        for t, v in sorted(by_tier_flip.items())
    ]
    lines += ["", "| dimension | flip rate | mean score spread (max - min) |", "|---|---|---|"]
    for dim in sorted(flips):
        f = flips[dim]
        spread = f"{statistics.mean(spreads[dim]):.2f}" if spreads.get(dim) else "n/a (check)"
        lines.append(f"| {dim} | {sum(f)}/{len(f)} ({sum(f) / len(f):.0%}) | {spread} |")
    lines += [
        "",
        "Replies were drafted through the real graph (rules-only triage, placeholder judge); "
        "the judge "
        "under test is the production `SeverityAwareJudge` (S3 = two-judge panel).",
        "",
    ]
    (ROOT / "docs" / "judge_consistency.md").write_text("\n".join(lines), encoding="utf-8")
    print("report: docs/judge_consistency.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
