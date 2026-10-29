"""Play one synthetic scenario through the full conversation graph with live free-tier models.

  uv run python scripts/demo_conversation.py SC-0001                     # with the real judge
  uv run python scripts/demo_conversation.py SC-0001 --follow "Can I get a refund instead?"
  uv run python scripts/demo_conversation.py SC-0001 --code-checks-only  # placeholder judge

Prints the passenger message, the final reply and the pipeline trace (triage, entitlement, checks,
judge scores and decisions).
"""

from __future__ import annotations

import argparse
import sys

from flightright.agent.judging import CodeChecksOnlyJudge, Judge
from flightright.agent.live import live_engine, load_scenarios, service_for
from flightright.config import load_config, load_env
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient
from flightright.triage.triage import Triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario_id")
    parser.add_argument(
        "--follow", action="append", default=[], help="follow-up passenger message(s)"
    )
    parser.add_argument("--code-checks-only", action="store_true", help="use the placeholder judge")
    args = parser.parse_args()

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    s = scenarios.get(args.scenario_id)
    if s is None or s.split == "test":
        print(f"unknown or held-out (test split) scenario {args.scenario_id}", file=sys.stderr)
        return 2

    cfg = load_config()
    load_env()
    client = LLMClient.from_config(cfg.models)
    judge: Judge = (
        CodeChecksOnlyJudge() if args.code_checks_only else SeverityAwareJudge.for_client(client)
    )
    service, cid = service_for(s, cfg, client, judge, live_engine(), Triage(client))

    for n, text in enumerate([s.opening_message, *args.follow], 1):
        out = service.handle_message(cid, text, f"demo-{n}")
        st = service.state(cid)
        print(f"\n=== PASSENGER ({s.persona.name}, gold tier {s.gold_tier})\n{text}")
        print(f"\n=== REPLY [{out.status}{', ticket ' + out.ticket_id if out.ticket_id else ''}]")
        print(out.reply_text)
        if st.get("triage"):
            t = st["triage"]
            print(
                f"\n--- triage {t['tier']} (rules {t['rules_tier']}, llm {t['llm_tier']}), "
                f"urgency {t['urgency']}"
            )
        if st.get("entitlement"):
            e = st["entitlement"]
            print(
                f"--- entitlement {e['regime']}: EUR {e['compensation_eur']} ({e['confidence']}); "
                f"gold EUR {s.gold_entitlement.compensation_eur}"
            )
        for a, v in zip(st.get("attempts") or [], st.get("verdicts") or [], strict=False):
            failed = [c["check_id"] for c in a["code_checks"] if not c["passed"]]
            j = v.get("judgement", {})
            scores = {
                x["id"]: x["score"] if x["kind"] == "scale" else x["passed"]
                for x in j.get("scores", [])
            }
            print(
                f"--- draft {a['attempt_no']} by {a['model']}: checks failed {failed or 'none'}; "
                f"judge {j.get('decision', v['decision'])} ({j.get('decision_reason', '')})"
            )
            if scores:
                print(f"    scores {scores}")
        for action in st.get("actions") or []:
            print(f"--- action: {action}")
        if st.get("ticket"):
            print(
                f"--- handoff: {st['ticket']['reason']}; "
                f"next step: {st['ticket']['recommended_next_step']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
