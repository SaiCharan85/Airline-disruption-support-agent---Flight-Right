"""Generate synthetic scenarios (SPEC §10.1): design in code, opening messages by a free LLM.

  uv run python scripts/generate_scenarios.py --dry-run   # plan + request/token estimate, no calls
  uv run python scripts/generate_scenarios.py --pilot 50  # first 50 scenarios + review file
  uv run python scripts/generate_scenarios.py             # everything (resumes where it stopped)

Output (git-ignored): data/scenarios/scenarios.jsonl, data/scenarios/pilot_review.md,
data/scenarios/manifest.json (seed, config, models, counts).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from flightright.config import ROOT, load_config, load_env
from flightright.llm.client import LLMClient
from flightright.llm.types import LLMUnavailableError
from flightright.models import Scenario
from flightright.rules.engine import EntitlementEngine
from flightright.scenarios.design import PlanConfig, build_plan
from flightright.scenarios.writer import ROLE, describe, estimate_batch_tokens, write_batch

OUT = ROOT / "data" / "scenarios"


def _engine() -> EntitlementEngine:
    full = ROOT / "data" / "ourairports" / "airports.csv"
    return EntitlementEngine.from_config(
        ROOT, airports_csv=full if full.is_file() else ROOT / "tests" / "fixtures" / "airports.csv"
    )


def _done(path: Path) -> dict[str, Scenario]:
    if not path.is_file():
        return {}
    rows = (
        Scenario.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )
    return {s.scenario_id: s for s in rows}


def _summary(scenarios: list[Scenario]) -> str:
    tiers = Counter(s.gold_tier for s in scenarios)
    regimes = Counter(s.gold_entitlement.regime for s in scenarios)
    splits = Counter(s.split for s in scenarios)
    groups = len({s.counterfactual_group for s in scenarios if s.counterfactual_group})
    templates = len({s.template_id for s in scenarios})
    return (
        f"{len(scenarios)} scenarios | tiers {dict(sorted(tiers.items()))} | "
        f"regimes {dict(regimes)} | "
        f"splits {dict(splits)} | {templates} templates | {groups} counterfactual groups"
    )


def _review(scenarios: list[Scenario], path: Path, warnings: dict[str, list[str]]) -> None:
    lines = [
        "# Scenario pilot review",
        "",
        "Synthetic data. Check: does the message sound like a real passenger, "
        "match the persona, and stick to the facts?",
        "",
    ]
    for s in scenarios:
        e = s.gold_entitlement
        comp = f"EUR {e.compensation_eur}" if e.compensation_eur else "none"
        lines += [
            f"## {s.scenario_id} · {s.gold_tier} · {s.template_id} · {e.regime}",
            f"*{s.persona.name}: {s.persona.temperament}, English {s.persona.english}, "
            f"{s.persona.honesty}"
            f"{', injection attempt' if s.persona.injection_attempt else ''}*  ",
            f"Gold: compensation {comp} ({e.confidence}); actions: {', '.join(s.gold_key_actions)}",
            "",
            f"> {s.opening_message}",
            "",
        ]
        if s.scenario_id in warnings:
            lines += [f"⚠ checks: {'; '.join(warnings[s.scenario_id])}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-base", type=int, default=360)
    parser.add_argument(
        "--groups", type=int, default=35, help="counterfactual groups (4 variants each)"
    )
    parser.add_argument("--pilot", type=int, default=0, help="only the first N scenarios")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    engine = _engine()
    plan = build_plan(
        engine, PlanConfig(n_base=args.n_base, counterfactual_groups=args.groups, seed=cfg.seed)
    )
    todo_all = plan.scenarios[: args.pilot] if args.pilot else plan.scenarios
    out_file = OUT / "scenarios.jsonl"
    done = _done(out_file)
    todo = [s for s in todo_all if s.scenario_id not in done]
    batches = [todo[i : i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    print(f"plan: {_summary(plan.scenarios)}")
    print(
        f"selected {len(todo_all)}, already written {len(todo_all) - len(todo)}, "
        f"to write {len(todo)} "
        f"in {len(batches)} requests"
    )

    load_env()
    client = LLMClient.from_config(cfg.models)
    first = cfg.models.roles[ROLE].chain[0]
    limits = cfg.models.catalog[first]
    tokens = sum(estimate_batch_tokens(client, b, engine.airports) for b in batches)
    minutes = math.ceil(len(batches) / max(1, int((limits.rpm or 60) * cfg.models.safety_margin)))
    req_left, tok_left = client.ledger.headroom(first)
    print(
        f"estimate: {len(batches)} requests, ~{tokens:,} tokens (worst case) on {first}; "
        f"today's headroom {req_left:.0f} requests / {tok_left:,.0f} tokens; "
        f">= {minutes} min at the RPM limit"
    )
    if args.dry_run:
        if todo:
            print("\nexample facts sent to the model:\n" + describe(todo[0], engine.airports))
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    all_warnings: dict[str, list[str]] = {}
    models_used: Counter[str] = Counter()
    with out_file.open("a", encoding="utf-8") as fh:
        for n, batch in enumerate(batches, 1):
            try:
                res = write_batch(client, batch, engine.airports)
            except LLMUnavailableError as exc:
                print(
                    f"stopping (free-tier budget or providers unavailable): {exc}", file=sys.stderr
                )
                break
            for s in batch:
                if s.scenario_id in res.messages:
                    fh.write(
                        s.model_copy(
                            update={"opening_message": res.messages[s.scenario_id]}
                        ).model_dump_json()
                        + "\n"
                    )
            fh.flush()
            all_warnings |= res.warnings
            if res.meta:
                models_used[res.meta.model] += 1
            print(
                f"[{n}/{len(batches)}] wrote {len(res.messages)}/{len(batch)}"
                f"{' via ' + res.meta.model if res.meta else ''}"
                f"{' (cached)' if res.meta and res.meta.cached else ''}"
                f"{'; missing ' + ', '.join(res.missing) if res.missing else ''}"
            )

    written = _done(out_file)
    selected = [written[s.scenario_id] for s in todo_all if s.scenario_id in written]
    if args.pilot:
        _review(selected, OUT / "pilot_review.md", all_warnings)
        print(f"review file: {OUT / 'pilot_review.md'}")
    (OUT / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "seed": cfg.seed,
                "n_base": args.n_base,
                "groups": args.groups,
                "written": len(written),
                "models": dict(models_used),
                "role_chain": cfg.models.roles[ROLE].chain,
                "warnings": {k: v for k, v in all_warnings.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"done: {_summary(selected)}; warnings on {len(all_warnings)} messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
