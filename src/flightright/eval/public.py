"""Public human-judgment checks (docs/eval_protocol.md §3): how the judge model agrees with human
raters on general-domain data. Not airline validation; reported separately.

- HelpSteer3 preference (nvidia/HelpSteer3, CC-BY-4.0): pairwise, judged in both orders.
- HelpSteer2 (nvidia/HelpSteer2, CC-BY-4.0): five 0-4 attribute ratings per response.

Rows come from the Hugging Face datasets-server API (free, no account needed).
"""

from __future__ import annotations

import ast
import json
import random
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from flightright.eval.metrics import rate, spearman
from flightright.llm.client import LLMClient

ROWS_URL = "https://datasets-server.huggingface.co/rows"
ATTRIBUTES = ("helpfulness", "correctness", "coherence", "complexity", "verbosity")
MAX_CHARS = 2000


def fetch_rows(
    dataset: str, config: str, split: str, http: httpx.Client | None = None, page: int = 100
) -> list[dict[str, Any]]:
    client = http or httpx.Client(timeout=60)
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = client.get(
            ROWS_URL,
            params={
                "dataset": dataset,
                "config": config,
                "split": split,
                "offset": offset,
                "length": page,
            },
        )
        r.raise_for_status()
        data = r.json()
        rows += [x["row"] for x in data["rows"]]
        offset += page
        if offset >= data["num_rows_total"] or not data["rows"]:
            return rows


def cached_rows(path: Path, fetch: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if path.is_file():
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    rows = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows


def _turns(context: Any) -> list[dict[str, str]]:
    if isinstance(context, str):
        try:
            context = ast.literal_eval(context)
        except (ValueError, SyntaxError):
            return [{"role": "user", "content": context}]
    return [{"role": str(t["role"]), "content": str(t["content"])} for t in context]


def select_helpsteer3(rows: Sequence[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """English general-domain pairs with a human preference, short enough for the free tier."""
    pool = []
    for i, r in enumerate(rows):
        pref = int(r["overall_preference"])
        turns = _turns(r["context"])
        prompt = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in turns)
        if (
            r.get("domain") == "general"
            and r.get("language") == "english"
            and pref != 0
            and len(prompt) <= MAX_CHARS
            and max(len(r["response1"]), len(r["response2"])) <= MAX_CHARS
        ):
            pool.append(
                {
                    "id": f"hs3-{i}",
                    "prompt": prompt,
                    "response1": r["response1"],
                    "response2": r["response2"],
                    "human": 1 if pref < 0 else 2,  # negative: response 1 is better
                    "strength": abs(pref),
                }
            )
    random.Random(seed).shuffle(pool)
    return pool[:n]


def select_helpsteer2(rows: Sequence[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """Responses stratified by the human helpfulness rating (0-4)."""
    by_level: dict[int, list[dict[str, Any]]] = {k: [] for k in range(5)}
    for i, r in enumerate(rows):
        if len(r["prompt"]) <= MAX_CHARS and len(r["response"]) <= MAX_CHARS:
            by_level[int(r["helpfulness"])].append(
                {
                    "id": f"hs2-{i}",
                    "prompt": r["prompt"],
                    "response": r["response"],
                    **{a: int(r[a]) for a in ATTRIBUTES},
                }
            )
    rng = random.Random(seed)
    for v in by_level.values():
        rng.shuffle(v)
    out: list[dict[str, Any]] = []
    while len(out) < n and any(by_level.values()):
        for level in range(5):
            if by_level[level] and len(out) < n:
                out.append(by_level[level].pop())
    return out


class PairwiseChoice(BaseModel):
    reason: str = Field(description="One or two sentences comparing the responses")
    better: Literal["A", "B"]


class AttributeRatings(BaseModel):
    helpfulness: int = Field(ge=0, le=4)
    correctness: int = Field(ge=0, le=4)
    coherence: int = Field(ge=0, le=4)
    complexity: int = Field(ge=0, le=4)
    verbosity: int = Field(ge=0, le=4)


PAIRWISE = (
    "You compare two assistant responses to the same conversation. Decide which response a careful "
    "human rater would prefer overall: more helpful, correct, and clear for what the user asked. "
    "Do not prefer a response for being longer or for being shown first."
)
RATING = (
    "You rate one assistant response on five attributes, each 0-4 (HelpSteer2 definitions). "
    "helpfulness: overall usefulness to the user. correctness: includes all relevant facts "
    "without errors. coherence: consistency and clarity of expression. complexity: intellectual "
    "depth required to write it (0 = anyone could, 4 = domain expert). verbosity: amount of detail "
    "relative to what was asked (0 = terse, 4 = very verbose)."
)


def judge_pair(
    client: LLMClient, item: dict[str, Any], role: str = "judge_a", use_cache: bool = True
) -> dict[str, Any]:
    """Both orders. A pair counts as correct only if both orders pick the human-preferred side."""
    picks = []
    models = []
    for order in ((1, 2), (2, 1)):
        a, b = (item[f"response{k}"] for k in order)
        user = f"CONVERSATION:\n{item['prompt']}\n\nRESPONSE A:\n{a}\n\nRESPONSE B:\n{b}"
        res = client.structured(role, PAIRWISE, user, PairwiseChoice, use_cache=use_cache)
        picks.append(order[0] if res.value.better == "A" else order[1])
        models.append(res.meta.model)
    return {
        "id": item["id"],
        "human": item["human"],
        "strength": item["strength"],
        "picks": picks,
        "consistent": picks[0] == picks[1],
        "correct": picks[0] == picks[1] == item["human"],
        "models": models,
    }


def rate_response(
    client: LLMClient, item: dict[str, Any], role: str = "judge_a", use_cache: bool = True
) -> dict[str, Any]:
    user = f"PROMPT:\n{item['prompt']}\n\nRESPONSE:\n{item['response']}"
    res = client.structured(role, RATING, user, AttributeRatings, use_cache=use_cache)
    return {
        "id": item["id"],
        "human": {a: item[a] for a in ATTRIBUTES},
        "judge": res.value.model_dump(),
        "model": res.meta.model,
    }


def pairwise_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "accuracy_both_orders": rate(sum(r["correct"] for r in results), len(results)),
        "position_consistency": rate(sum(r["consistent"] for r in results), len(results)),
        "accuracy_strong_preferences": rate(
            sum(r["correct"] for r in results if r["strength"] >= 2),
            sum(1 for r in results if r["strength"] >= 2),
        ),
    }


def rating_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        a: {
            "spearman": spearman(
                [float(r["human"][a]) for r in results], [float(r["judge"][a]) for r in results]
            ),
            "n": len(results),
        }
        for a in ATTRIBUTES
    }
