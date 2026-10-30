"""Public human-judgment checks with scripted models and a mocked datasets-server."""

import json
from typing import Any

import httpx

from flightright.config import RoleConfig
from flightright.eval.public import (
    fetch_rows,
    judge_pair,
    pairwise_summary,
    rate_response,
    rating_summary,
    select_helpsteer2,
    select_helpsteer3,
)
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.usage import UsageLedger


def client(answers: list[Any]) -> tuple[LLMClient, ScriptedProvider]:
    cfg = models_cfg(roles={"judge_a": RoleConfig(chain=["a:m1"], max_tokens=100)})
    p = ScriptedProvider("a", answers)
    return (
        LLMClient(
            cfg, {"a": p}, LLMCache(":memory:"), UsageLedger(":memory:", cfg.catalog, 1.0, 0)
        ),
        p,
    )


def hs3_row(pref: int, domain: str = "general", n: int = 100) -> dict[str, Any]:
    return {
        "domain": domain,
        "language": "english",
        "context": str([{"role": "user", "content": "Explain tides"}]),
        "response1": "r1 " * n,
        "response2": "r2",
        "overall_preference": str(pref),
    }


def test_fetch_rows_pages_through_the_datasets_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        rows = [{"row": {"i": i}} for i in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json={"rows": rows, "num_rows_total": 5})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert [r["i"] for r in fetch_rows("d", "c", "s", http=http, page=2)] == [0, 1, 2, 3, 4]


def test_select_helpsteer3_filters_ties_domains_and_length() -> None:
    rows = [hs3_row(-2), hs3_row(0), hs3_row(3, domain="code"), hs3_row(1, n=2000), hs3_row(2)]
    sel = select_helpsteer3(rows, n=10, seed=1)
    assert sorted((s["human"], s["strength"]) for s in sel) == [(1, 2), (2, 2)]
    assert "USER: Explain tides" in sel[0]["prompt"]


def test_select_helpsteer2_stratifies_by_helpfulness() -> None:
    rows = [
        {
            "prompt": "p",
            "response": "r",
            "helpfulness": str(h),
            "correctness": "3",
            "coherence": "4",
            "complexity": "1",
            "verbosity": "2",
        }
        for h in [0, 0, 1, 2, 3, 4, 4, 4]
    ]
    sel = select_helpsteer2(rows, n=5, seed=1)
    assert sorted(s["helpfulness"] for s in sel) == [0, 1, 2, 3, 4]


def test_judge_pair_needs_both_orders_to_agree_with_the_human() -> None:
    item = {
        "id": "x",
        "prompt": "q",
        "response1": "good",
        "response2": "bad",
        "human": 1,
        "strength": 2,
    }
    c, p = client(
        [json.dumps({"reason": "a", "better": "A"}), json.dumps({"reason": "b", "better": "B"})]
    )
    r = judge_pair(c, item)
    assert r["picks"] == [1, 1] and r["consistent"] and r["correct"]
    assert "RESPONSE A:\nbad" in p.requests[1].messages[1].content  # swapped order
    c2, _ = client([json.dumps({"reason": "a", "better": "A"})] * 2)
    r2 = judge_pair(c2, item)  # always picks the first shown: position bias
    assert r2["picks"] == [1, 2] and not r2["consistent"] and not r2["correct"]
    s = pairwise_summary([r, r2])
    assert s["accuracy_both_orders"]["rate"] == 0.5 and s["position_consistency"]["rate"] == 0.5


def test_rate_response_and_rank_correlation() -> None:
    items = [
        {
            "id": f"i{h}",
            "prompt": "p",
            "response": f"response {h}",  # distinct, or the exact-match cache answers
            "helpfulness": h,
            "correctness": h,
            "coherence": 4,
            "complexity": 1,
            "verbosity": h,
        }
        for h in range(4)
    ]
    answers = [
        json.dumps(
            {
                "helpfulness": h,
                "correctness": h,
                "coherence": 3,
                "complexity": 1,
                "verbosity": 3 - h,
            }
        )
        for h in range(4)
    ]
    c, _ = client(answers)
    results = [rate_response(c, it) for it in items]
    s = rating_summary(results)
    assert s["helpfulness"]["spearman"] == 1.0 and s["verbosity"]["spearman"] == -1.0
    assert s["coherence"]["spearman"] is None  # constant human ratings
