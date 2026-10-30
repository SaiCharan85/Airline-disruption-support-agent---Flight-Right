"""Running the evaluation (docs/eval_protocol.md §7): draft base replies, judge items, resume.

Every phase appends one JSON line per finished unit, so an interrupted run resumes without
re-calling the models.
"""

from __future__ import annotations

import json
import random
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from flightright.agent.judging import CodeChecksOnlyJudge, Judge
from flightright.agent.live import service_for
from flightright.config import AppConfig
from flightright.eval.items import BaseReply, CheckInputsRecord, EvalItem, check_results
from flightright.llm.client import LLMClient
from flightright.models import CheckResult, Entitlement, Scenario, TriageResult
from flightright.rules.engine import EntitlementEngine
from flightright.triage.triage import Triage


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def stratified(
    scenarios: Sequence[Scenario], per_tier: dict[str, int], seed: int
) -> list[Scenario]:
    """Seeded sample per gold tier, counterfactual variants excluded."""
    by_tier: dict[str, list[Scenario]] = defaultdict(list)
    for s in scenarios:
        if s.counterfactual_group is None:
            by_tier[s.gold_tier].append(s)
    rng = random.Random(seed)
    out: list[Scenario] = []
    for tier in sorted(per_tier):
        pool = sorted(by_tier[tier], key=lambda s: s.scenario_id)
        rng.shuffle(pool)
        out += pool[: per_tier[tier]]
    return out


def draft_until(
    scenarios: Sequence[Scenario],
    per_tier: dict[str, int],
    seed: int,
    path: Path,
    draft: Callable[[Scenario], tuple[BaseReply | None, str]],
    max_factor: int = 3,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Draft each tier in its seeded order until `per_tier[t]` usable replies, trying at most
    `max_factor` times that many scenarios (cases the engine sends to review are not usable)."""
    pools = {t: stratified(scenarios, {t: 10**6}, seed) for t in per_tier}
    rows: list[dict[str, Any]] = []
    for tier, target in sorted(per_tier.items()):
        usable = 0
        for s in pools[tier][: target * max_factor]:
            if usable >= target:
                break
            row = _draft_one(s, path, draft, log)
            rows.append(row)
            usable += row["base"] is not None
    return rows


def _draft_one(
    s: Scenario,
    path: Path,
    draft: Callable[[Scenario], tuple[BaseReply | None, str]],
    log: Callable[[str], None],
) -> dict[str, Any]:
    done = {r["scenario_id"]: r for r in read_jsonl(path)}
    if s.scenario_id in done:
        return done[s.scenario_id]
    base, status = draft(s)
    row = {
        "scenario_id": s.scenario_id,
        "status": status,
        "base": base.model_dump() if base else None,
    }
    append_jsonl(path, row)
    log(f"  drafted {s.scenario_id} ({s.gold_tier}): {status}")
    return row


def counterfactual_groups(
    scenarios: Sequence[Scenario], n_groups: int, seed: int
) -> dict[str, list[Scenario]]:
    groups: dict[str, list[Scenario]] = defaultdict(list)
    for s in scenarios:
        if s.counterfactual_group:
            groups[s.counterfactual_group].append(s)
    names = sorted(groups)
    random.Random(seed).shuffle(names)
    return {g: sorted(groups[g], key=lambda s: s.scenario_id) for g in sorted(names[:n_groups])}


def draft_base(
    s: Scenario, cfg: AppConfig, client: LLMClient, engine: EntitlementEngine
) -> tuple[BaseReply | None, str]:
    """One reply through the real graph (rules-only triage, code checks, no LLM judge).
    Returns (base reply or None, status)."""
    service, cid = service_for(s, cfg, client, CodeChecksOnlyJudge(), engine, Triage(None))
    out = service.handle_message(cid, s.opening_message, "eval-1")
    st = service.state(cid)
    if out.status != "answered" or not st.get("attempts"):
        return None, out.status
    checks = st["attempts"][-1]["code_checks"]
    if not all(c["passed"] for c in checks):
        return None, "code_checks_failed"
    ent = Entitlement.model_validate(st["entitlement"])
    waiting = st["context"].get("days_until_departure") == 0
    base = BaseReply(
        scenario_id=s.scenario_id,
        split=s.split,
        gold_tier=s.gold_tier,
        draft=out.reply_text,
        transcript=st["transcript"][:-1],
        triage=st["triage"],
        entitlement=st["entitlement"],
        checks=checks,
        check_inputs=CheckInputsRecord(
            voucher_amounts=list(st.get("voucher_amounts", [])),
            refund_amount=st.get("refund_amount"),
            care_expected=bool(ent.care) and waiting,
            allowed_timeline_days=[ent.refund_or_rerouting.refund_within_days]
            if ent.refund_or_rerouting
            else [7],
        ),
        counterfactual_group=s.counterfactual_group,
        variant=s.variant,
    )
    return base, "answered"


def draft_all(
    scenarios: Iterable[Scenario],
    path: Path,
    draft: Callable[[Scenario], tuple[BaseReply | None, str]],
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Resumable: one line per scenario (`base` is null when the draft was not usable)."""
    done = {r["scenario_id"]: r for r in read_jsonl(path)}
    for s in scenarios:
        if s.scenario_id in done:
            continue
        base, status = draft(s)
        row = {
            "scenario_id": s.scenario_id,
            "status": status,
            "base": base.model_dump() if base else None,
        }
        append_jsonl(path, row)
        done[s.scenario_id] = row
        log(f"  drafted {s.scenario_id} ({s.gold_tier}): {status}")
    return list(done.values())


def judge_one(
    judge: Judge,
    item: EvalItem,
    base: BaseReply,
    *,
    judge_only: bool = False,
) -> dict[str, Any]:
    """Judge one item at the item's (gold) tier. `judge_only` tells the judge every code check
    passed, to measure what it would catch on its own."""
    triage = TriageResult.model_validate({**base.triage, "tier": item.tier})
    checks = check_results(item)
    if judge_only:
        checks = [CheckResult(check_id=c.check_id, passed=True, detail="") for c in checks]
    v = judge.evaluate(
        draft=item.reply,
        transcript=base.transcript,
        triage=triage,
        entitlement=Entitlement.model_validate(base.entitlement),
        checks=checks,
        target="reply",
    )
    j = v.judgement
    reason = str(j.get("decision_reason", ""))
    return {
        **item.model_dump(),
        "decision": v.decision,
        "decision_reason": reason,
        "caught_by": "code" if reason.startswith("code check failed") else "judge",
        "scores": j.get("scores", []),
        "panel": j.get("panel"),
        "models": j.get("models", {}),
        "error": j.get("error"),
        "latency_ms": j.get("latency_ms", 0),
        "requests": j.get("model_calls", 0),  # model calls incl. evidence/validation retries
        "judge_only": judge_only,
    }


def judge_all(
    items: Sequence[EvalItem],
    bases: dict[str, BaseReply],
    path: Path,
    judge: Judge,
    *,
    judge_only: bool = False,
    workers: int = 1,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Resumable. With `workers` > 1 items are judged in parallel threads; the client's usage
    ledger keeps every model under its per-minute limits."""
    done = {r["item_id"]: r for r in read_jsonl(path)}
    todo = [i for i in items if i.item_id not in done]
    lock = threading.Lock()

    def one(item: EvalItem) -> None:
        row = judge_one(judge, item, bases[item.base_id], judge_only=judge_only)
        with lock:
            append_jsonl(path, row)
            done[item.item_id] = row
            log(f"  [{len(done)}/{len(items)}] {item.item_id} {item.tier}: {row['decision']}")

    if workers <= 1:
        for item in todo:
            one(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for f in [pool.submit(one, i) for i in todo]:
                f.result()
    return [done[i.item_id] for i in items if i.item_id in done]


def engine_fairness(groups: dict[str, list[Scenario]], engine: EntitlementEngine) -> dict[str, Any]:
    """Entitlements must not differ within a counterfactual group (no model calls)."""
    out: dict[str, Any] = {}
    for g, variants in groups.items():
        ents = {
            s.scenario_id: engine.compute_entitlement(s.case).model_dump(
                mode="json", exclude={"reason_trace"}
            )
            for s in variants
        }
        distinct = {json.dumps(e, sort_keys=True) for e in ents.values()}
        out[g] = {"variants": len(variants), "distinct_entitlements": len(distinct)}
    return {
        "groups": out,
        "groups_with_differences": sum(1 for v in out.values() if v["distinct_entitlements"] > 1),
    }
