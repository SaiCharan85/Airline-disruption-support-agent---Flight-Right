"""Re-judge items whose verdict failed for infrastructure reasons (docs/eval_protocol.md, change 1).

  uv run python scripts/eval_rejudge.py --dry-run
  uv run python scripts/eval_rejudge.py

Rate limits, provider 5xx errors and timeouts say nothing about the judge, so those items are
judged once more, for both systems, into separate files (judged_<system>_rejudged.jsonl). Content
failures (invalid output, evidence quotes not found in the draft) are the judge's own and are kept.
The original records are never modified; the report shows both.
"""

from __future__ import annotations

import argparse
import re
import sys

from flightright.config import ROOT, load_config, load_env
from flightright.eval import items as ev
from flightright.eval import runner as rn
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient

OUT = ROOT / "data" / "eval" / "final"
INFRA = re.compile(
    r"rate limited|\b429\b|HTTP 5\d\d|\"code\": 5\d\d|ReadTimeout|timed out|ConnectError"
)
CONTENT = re.compile(r"evidence not found|invalid output")


def infrastructure_failure(error: str | None) -> bool:
    """True when every model in the chain failed for reasons outside the judge's answer."""
    if not error:
        return False
    if "evidence not found" in error:
        return False
    if CONTENT.search(error) and not INFRA.search(error):
        return False
    return bool(INFRA.search(error))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    items = {i["item_id"]: ev.EvalItem(**i) for i in rn.read_jsonl(OUT / "items.jsonl")}
    bases = {
        r["scenario_id"]: ev.BaseReply(**r["base"])
        for r in rn.read_jsonl(OUT / "bases.jsonl")
        if r["base"]
    }
    todo: dict[str, list[ev.EvalItem]] = {}
    for system in ("aware", "fixed"):
        seen: dict[str, dict[str, object]] = {}
        for r in rn.read_jsonl(OUT / f"judged_{system}.jsonl"):
            seen.setdefault(r["item_id"], r)
        todo[system] = [
            items[i]
            for i, r in seen.items()
            if i in items and infrastructure_failure(str(r.get("error") or ""))
        ]
        print(f"{system}: {len(todo[system])} items to re-judge")
    if args.dry_run:
        return 0
    cfg = load_config()
    load_env()
    client = LLMClient.from_config(cfg.models)
    judges = {
        "aware": SeverityAwareJudge.for_client(client, use_cache=False),
        "fixed": SeverityAwareJudge.for_client(
            client, use_cache=False, fixed_profile="S2", triage_context=False
        ),
    }
    for system, sel in todo.items():
        rn.judge_all(
            sel,
            bases,
            OUT / f"judged_{system}_rejudged.jsonl",
            judges[system],
            workers=args.workers,
        )
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
