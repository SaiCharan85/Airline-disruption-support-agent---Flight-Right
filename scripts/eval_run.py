"""Evaluation runs (docs/eval_protocol.md). Resumable; every phase writes JSONL under data/eval/.

  uv run python scripts/eval_run.py --pilot --dry-run          # dev pilot, request estimate
  uv run python scripts/eval_run.py --pilot                    # dev pilot (not reported)
  uv run python scripts/eval_run.py --split test --final --dry-run
  uv run python scripts/eval_run.py --split test --final       # the one final run
  uv run python scripts/eval_run.py --split test --final --phase public

Phases (default: all, in this order): bases, headline, judge_only, bias, public, fairness.
Triage and the simulator have their own scripts (eval_triage.py, simulate.py); eval_report.py
collects everything into eval/report.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from flightright.agent.live import live_engine, load_scenarios
from flightright.config import ROOT, load_config, load_env
from flightright.eval import items as ev
from flightright.eval import public
from flightright.eval import runner as rn
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient

PHASES = ("bases", "headline", "judge_only", "bias", "public", "fairness")
CORRECTNESS_ERRORS = {"wrong_amount", "wrong_regime", "fake_extraordinary", "voucher_push"}


def freeze_protocol(out: Any) -> None:
    """Record the protocol's hash before the first final-run call; refuse a changed protocol."""
    proto = ROOT / "docs" / "eval_protocol.md"
    digest = hashlib.sha256(proto.read_bytes()).hexdigest()
    path = out / "manifest.json"
    if path.is_file():
        frozen = json.loads(path.read_text(encoding="utf-8"))
        if frozen["protocol_sha256"] != digest:
            raise SystemExit(
                "docs/eval_protocol.md changed after the final run started; record the change "
                "under 'Changes after registration' and restore the frozen text"
            )
        return
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "protocol": "docs/eval_protocol.md",
                "protocol_sha256": digest,
                "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"protocol frozen: sha256 {digest[:16]}...")


def plan(args: argparse.Namespace) -> dict[str, Any]:
    if args.pilot:
        return {
            "per_tier": {"S1": 3, "S2": 3, "S3": 4},
            "per_base": 2,
            "bias_n": 4,
            "public_n": 6,
            "fair_groups": 2,
        }
    return {
        "per_tier": {"S1": 12, "S2": 12, "S3": 16},
        "per_base": 2,
        "bias_n": 16,
        "public_n": 30,
        "fair_groups": 6,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--final", action="store_true", help="allow the held-out test split")
    parser.add_argument("--pilot", action="store_true", help="small dev run to debug the harness")
    parser.add_argument("--phase", choices=PHASES, action="append")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=4, help="parallel judge calls")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        print("refusing the test split without --final", file=sys.stderr)
        return 2
    if args.pilot and args.split != "dev":
        print("the pilot runs on dev", file=sys.stderr)
        return 2
    phases = args.phase or list(PHASES)
    p = plan(args)
    out = ROOT / "data" / "eval" / ("final" if args.split == "test" else "pilot")
    cfg = load_config()
    seed = cfg.seed

    scenarios = load_scenarios(args.split)
    groups = rn.counterfactual_groups(scenarios, p["fair_groups"], seed)
    n_base = sum(p["per_tier"].values())
    n_items = n_base * (1 + p["per_base"])
    s3_items = p["per_tier"].get("S3", 0) * (1 + p["per_base"])
    n_variants = sum(len(v) for v in groups.values())
    est = {
        "bases (agent drafts; up to 3x if cases go to review)": n_base,
        "headline: severity-aware (S3 panel = 2)": n_items + s3_items,
        "headline: fixed-strictness": n_items,
        "judge_only (correctness errors, S2)": p["per_tier"].get("S2", 0),
        "bias (2 probes per reply)": 2 * p["bias_n"],
        "public (pairs x2 orders + ratings)": 3 * p["public_n"],
        "fairness (drafts + judging)": n_variants * 2,
    }
    print(
        f"{args.split}{' pilot' if args.pilot else ''}: {n_base} base scenarios "
        f"{p['per_tier']}, {len(groups)} counterfactual groups ({n_variants} variants) -> {out}"
    )
    for k, v in est.items():
        print(f"  ~{v:4d} requests  {k}")
    print(f"  ~{sum(est.values())} requests in total, before evidence retries and cache hits")
    if args.dry_run:
        return 0

    if args.final:
        freeze_protocol(out)
    load_env()
    client = LLMClient.from_config(cfg.models)
    engine = live_engine()
    use_cache = not args.final  # the final run bypasses the cache (protocol §2)
    aware = SeverityAwareJudge.for_client(client, use_cache=use_cache)
    fixed = SeverityAwareJudge.for_client(
        client, use_cache=use_cache, fixed_profile="S2", triage_context=False
    )

    def draft(s: Any) -> tuple[ev.BaseReply | None, str]:
        return rn.draft_base(s, cfg, client, engine)

    def load_bases(name: str, scen: list[Any]) -> dict[str, ev.BaseReply]:
        rows = rn.draft_all(scen, out / name, draft)
        return {r["scenario_id"]: ev.BaseReply(**r["base"]) for r in rows if r["base"]}

    bases: dict[str, ev.BaseReply] = {}
    if set(phases) & {"bases", "headline", "judge_only", "bias"}:
        print("phase bases")
        rows = rn.draft_until(scenarios, p["per_tier"], seed, out / "bases.jsonl", draft)
        bases = {r["scenario_id"]: ev.BaseReply(**r["base"]) for r in rows if r["base"]}
        print(f"  {len(bases)}/{n_base} usable base replies from {len(rows)} drafted")
    order = list(bases.values())
    items = [ev.original(b) for b in order] + ev.planted_items(order, p["per_base"], seed)
    if items:
        (out / "items.jsonl").write_text(
            "".join(i.model_dump_json() + "\n" for i in items), encoding="utf-8"
        )

    run: dict[str, Callable[[], None]] = {}

    def headline() -> None:
        print("phase headline: severity-aware")
        rn.judge_all(items, bases, out / "judged_aware.jsonl", aware, workers=args.workers)
        print("phase headline: fixed-strictness")
        rn.judge_all(items, bases, out / "judged_fixed.jsonl", fixed, workers=args.workers)

    def judge_only() -> None:
        print("phase judge_only")
        sel = [
            i
            for i in items
            if i.kind == "planted" and i.error_type in CORRECTNESS_ERRORS and i.tier == "S2"
        ]
        rn.judge_all(
            sel,
            bases,
            out / "judged_judge_only.jsonl",
            aware,
            judge_only=True,
            workers=args.workers,
        )

    def bias() -> None:
        print("phase bias")
        pool = [b for b in order if b.gold_tier in ("S2", "S3")]
        half = p["bias_n"] // 2
        sel = [b for b in pool if b.gold_tier == "S2"][:half] + [
            b for b in pool if b.gold_tier == "S3"
        ][:half]
        rn.judge_all(
            ev.bias_items(sel, seed), bases, out / "judged_bias.jsonl", aware, workers=args.workers
        )

    def run_public(name: str, sel: list[dict[str, Any]], fn: Any) -> None:
        done = {r["id"] for r in rn.read_jsonl(out / name)}
        todo = [it for it in sel if it["id"] not in done]
        lock = threading.Lock()

        def one(it: dict[str, Any]) -> None:
            try:
                row = fn(client, it, "judge_a", use_cache)
            except Exception as exc:  # one unusable answer must not stop the run
                row = {"id": it["id"], "error": str(exc)[:300]}
            with lock:
                rn.append_jsonl(out / name, row)
                done.add(it["id"])
                print(f"  [{len(done)}/{len(sel)}] {name} {it['id']}", flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for f in [pool.submit(one, it) for it in todo]:
                f.result()

    def public_sets() -> None:
        print("phase public")
        cache = ROOT / "data" / "public"
        hs3 = public.cached_rows(
            cache / "helpsteer3_preference_validation.jsonl",
            lambda: public.fetch_rows("nvidia/HelpSteer3", "preference", "validation"),
        )
        hs2 = public.cached_rows(
            cache / "helpsteer2_validation.jsonl",
            lambda: public.fetch_rows("nvidia/HelpSteer2", "default", "validation"),
        )
        for name, sel, fn in (
            (
                "public_hs3.jsonl",
                public.select_helpsteer3(hs3, p["public_n"], seed),
                public.judge_pair,
            ),
            (
                "public_hs2.jsonl",
                public.select_helpsteer2(hs2, p["public_n"], seed),
                public.rate_response,
            ),
        ):
            run_public(name, sel, fn)

    def fairness() -> None:
        print("phase fairness")
        (out / "engine_fairness.json").write_text(
            json.dumps(
                rn.engine_fairness(rn.counterfactual_groups(scenarios, 999, seed), engine),
                indent=2,
            ),
            encoding="utf-8",
        )
        variants = [s for g in groups.values() for s in g]
        fb = load_bases("fairness_bases.jsonl", variants)
        fitems = [ev.original(b) for b in fb.values()]
        rn.judge_all(fitems, fb, out / "judged_fairness.jsonl", aware, workers=args.workers)

    run = {
        "headline": headline,
        "judge_only": judge_only,
        "bias": bias,
        "public": public_sets,
        "fairness": fairness,
    }
    for ph in phases:
        if ph in run:
            run[ph]()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
