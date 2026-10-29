"""Multi-turn simulations (FR-13): CrewAI passengers talk to the real graph; an oracle resolves
handoffs.

  uv run python scripts/simulate.py --n 5 --dry-run
  uv run python scripts/simulate.py --n 5                   # -> data/eval/simulations_dev.jsonl
  uv run python scripts/simulate.py --n 5 --oracle-noise 0.1

Dev split only (the test split is reserved for the final evaluation, behind --final).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter

from flightright.agent.live import live_engine, load_scenarios, service_for
from flightright.config import ROOT, load_config, load_env
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient
from flightright.simulator.crew import ClientCrewLLM
from flightright.simulator.runner import simulate
from flightright.triage.triage import Triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--max-turns", type=int, default=4)
    parser.add_argument("--oracle-noise", type=float, default=0.0)
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        print("refusing the test split without --final", file=sys.stderr)
        return 2

    cfg = load_config()
    rng = random.Random(cfg.seed)
    pool = [s for s in load_scenarios(args.split) if s.counterfactual_group is None]
    rng.shuffle(pool)
    chosen = pool[: args.n]
    per_turn = 3  # triage + draft + judge (more with revisions / the S3 panel)
    print(
        f"{len(chosen)} {args.split} scenarios {dict(Counter(s.gold_tier for s in chosen))}; up to "
        f"{args.max_turns} turns; roughly "
        f"{len(chosen) * args.max_turns * (per_turn + 1)} requests at most"
    )
    if args.dry_run:
        return 0

    load_env()
    client = LLMClient.from_config(cfg.models)
    engine = live_engine()
    llm = ClientCrewLLM(client)
    out_path = ROOT / "data" / "eval" / f"simulations_{args.split}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    outcomes = []
    with out_path.open("a", encoding="utf-8") as fh:
        for i, s in enumerate(chosen, 1):
            service, cid = service_for(
                s, cfg, client, SeverityAwareJudge.for_client(client), engine, Triage(client)
            )
            out = simulate(
                s,
                service,
                cid,
                llm,
                max_turns=args.max_turns,
                oracle_noise=args.oracle_noise,
                seed=cfg.seed,
            )
            outcomes.append(out)
            fh.write(out.model_dump_json() + "\n")
            print(
                f"[{i}/{len(chosen)}] {s.scenario_id} {s.gold_tier}: {out.n_turns} turns, ended by "
                f"{out.ended_by}"
                f"{', resolved: ' + out.resolution.decision if out.resolution else ''}"
            )
    ended = Counter(o.ended_by for o in outcomes)
    print(
        json.dumps(
            {
                "ended_by": dict(ended),
                "mean_turns": sum(o.n_turns for o in outcomes) / max(1, len(outcomes)),
                "handoff_rate": sum(o.handed_off for o in outcomes) / max(1, len(outcomes)),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
