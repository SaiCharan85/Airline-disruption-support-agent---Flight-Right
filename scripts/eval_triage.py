"""Evaluate triage on synthetic scenarios (Step 5): rules alone, LLM alone, and merged.

  uv run python scripts/eval_triage.py --rules-only           # free, no LLM calls
  uv run python scripts/eval_triage.py --n 50 --dry-run        # request/token estimate
  uv run python scripts/eval_triage.py --n 50                  # pilot
  uv run python scripts/eval_triage.py --report               # full dev split + docs/triage_eval.md

The test split is refused unless --final is given (it is touched once, at the final evaluation).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime

from flightright.config import ROOT, load_config, load_env
from flightright.llm.client import LLMClient
from flightright.models import Scenario
from flightright.triage.evaluation import TriageMetrics, context_for
from flightright.triage.rules import rule_triage
from flightright.triage.triage import ROLE, Triage, _user, instructions

SCENARIOS = ROOT / "data" / "scenarios" / "scenarios.jsonl"
OUT = ROOT / "data" / "eval"


def load(split: str) -> list[Scenario]:
    rows = [
        Scenario.model_validate_json(x)
        for x in SCENARIOS.read_text(encoding="utf-8").splitlines()
        if x
    ]
    return [s for s in rows if s.split == split]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--final", action="store_true", help="allow the held-out test split")
    parser.add_argument("--n", type=int, default=0)
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="write docs/triage_eval.md")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        print("refusing the test split without --final", file=sys.stderr)
        return 2

    scenarios = load(args.split)
    scenarios = scenarios[: args.n] if args.n else scenarios
    rules_m = TriageMetrics()
    for s in scenarios:
        rules_m.add(s.gold_tier, rule_triage(s.opening_message, context_for(s)).tier)
    print(
        f"{len(scenarios)} {args.split} scenarios | rules only: accuracy {rules_m.accuracy:.0%}, "
        f"recall@S3+ {rules_m.recall_s3_plus:.0%}, under-triage {rules_m.under_triage_rate:.0%}, "
        f"over-triage {rules_m.over_triage_rate:.0%}"
    )
    print(rules_m.table())
    if args.rules_only:
        return 0

    cfg = load_config()
    load_env()
    client = LLMClient.from_config(cfg.models)
    first = cfg.models.roles[ROLE].chain[0]
    tokens = sum(
        client.estimate_tokens(ROLE, instructions(), _user(s.opening_message, context_for(s)))
        for s in scenarios
    )
    lim = cfg.models.catalog[first]
    per_min = min(
        (lim.rpm or 60) * cfg.models.safety_margin,
        (lim.tpm or 10**9) * cfg.models.safety_margin / max(1, tokens / len(scenarios)),
    )
    req_left, _ = client.ledger.headroom(first)
    print(
        f"estimate: {len(scenarios)} requests, ~{tokens:,} tokens (worst case) on {first}; "
        f"today's headroom {req_left:.0f} requests; ~{math.ceil(len(scenarios) / per_min)} min"
    )
    if args.dry_run:
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    triage = Triage(client)
    llm_m, merged_m = TriageMetrics(), TriageMetrics()
    models: Counter[str] = Counter()
    rows, latencies = [], []
    for i, s in enumerate(scenarios, 1):
        start = time.perf_counter()
        result = triage.classify(s.opening_message, context_for(s))
        latencies.append(time.perf_counter() - start)
        merged_m.add(s.gold_tier, result.tier)
        if result.llm_tier:
            llm_m.add(s.gold_tier, result.llm_tier)
        meta = triage.last_meta
        models[meta.model if meta else "rules-only"] += 1
        rows.append(
            {
                "scenario_id": s.scenario_id,
                "gold": s.gold_tier,
                "rules": result.rules_tier,
                "llm": result.llm_tier,
                "merged": result.tier,
                "confidence": result.llm_confidence,
                "cached": bool(meta and meta.cached),
                "reasons": result.reasons,
            }
        )
        if i % 25 == 0:
            print(f"[{i}/{len(scenarios)}] merged recall@S3+ so far {merged_m.recall_s3_plus:.0%}")
    (OUT / f"triage_{args.split}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    summary = []
    for name, m in (("rules", rules_m), ("llm", llm_m), ("merged", merged_m)):
        summary.append(
            f"| {name} | {m.n} | {m.accuracy:.0%} | {m.recall_s3_plus:.0%} | "
            f"{m.under_triage_rate:.0%} | {m.over_triage_rate:.0%} |"
        )
        print(summary[-1])
    if args.report:
        lat = sorted(latencies)
        report = "\n".join(
            [
                "# Triage evaluation",
                "",
                f"Split: `{args.split}` ({len(scenarios)} synthetic scenarios). "
                f"Run {datetime.now(UTC):%Y-%m-%d}. "
                f"Models: {dict(models)}.",
                "",
                "Gold tiers come from the scenario design (`config/triage.yaml`); "
                "messages were written by "
                "gemini-3.5-flash-lite, triage runs gpt-oss-20b first to avoid self-preference.",
                "",
                "| method | n | accuracy | recall@S3+ | under-triage | over-triage |",
                "|---|---|---|---|---|---|",
                *summary,
                "",
                "Target: recall@S3+ >= 98 %. Under-triage = predicted tier below gold.",
                "",
                "## Merged (production) confusion matrix",
                "",
                merged_m.table(),
                "",
                "## Rules only",
                "",
                rules_m.table(),
                "",
                "## LLM only",
                "",
                llm_m.table(),
                "",
                f"Latency per message (incl. cache hits): p50 {lat[len(lat) // 2]:.2f} s, "
                f"p95 {lat[int(len(lat) * 0.95) - 1]:.2f} s.",
                "",
            ]
        )
        (ROOT / "docs" / "triage_eval.md").write_text(report, encoding="utf-8")
        print("report: docs/triage_eval.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
