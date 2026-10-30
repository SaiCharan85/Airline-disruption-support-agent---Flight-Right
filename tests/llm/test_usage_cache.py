from pathlib import Path
from typing import Any

import pytest

from flightright.config import ModelLimits
from flightright.llm.cache import LLMCache, cache_key
from flightright.llm.testing import FakeClock
from flightright.llm.types import BudgetExceededError, ChatRequest, ChatResult, Message
from flightright.llm.usage import UsageLedger, estimate_tokens


def _ledger(
    path: Any = ":memory:", clock: FakeClock | None = None, margin: float = 1.0, **lim: int
) -> UsageLedger:
    clock = clock or FakeClock()
    limits = {"m": ModelLimits(family="f", **({"rpm": 100, "rpd": 3, "tpd": 1000} | lim))}
    return UsageLedger(path, limits, margin, 0, clock=clock, sleep=clock.sleep)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 1 and estimate_tokens("x" * 400) == 101


def test_NFR_2_daily_request_cap_refuses() -> None:
    ledger = _ledger()
    for _ in range(3):
        ledger.reserve("m", 10)
        ledger.record("m", 10)
    with pytest.raises(BudgetExceededError, match="daily"):
        ledger.reserve("m", 10)


def test_daily_token_cap_and_margin() -> None:
    ledger = _ledger(margin=0.5)  # tpd 1000 -> 500 usable
    ledger.record("m", 450)
    with pytest.raises(BudgetExceededError):
        ledger.reserve("m", 100)


def test_counter_persists_and_resets_next_day(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "u.sqlite3"
    _ledger(path, clock).record("m", 10)
    assert _ledger(path, clock).today("m") == (1, 10)
    clock.t += 86_400
    assert _ledger(path, clock).today("m") == (0, 0)


def test_minute_window_waits_instead_of_failing() -> None:
    clock = FakeClock()
    ledger = _ledger(clock=clock, rpm=2, rpd=100, tpd=10**6)
    for _ in range(2):
        ledger.record("m", 1, ledger.reserve("m", 1))
    ledger.reserve("m", 1)  # third call in the same minute must wait ~60 s
    assert clock.slept and sum(clock.slept) >= 59


def test_minute_window_counts_calls_still_in_flight() -> None:
    """Parallel callers: an admitted call books its estimate before it finishes."""
    clock = FakeClock()
    ledger = _ledger(clock=clock, rpm=100, rpd=100, tpd=10**6, tpm=100)
    first = ledger.reserve("m", 60)  # in flight, not yet recorded
    ledger.reserve("m", 60)  # would exceed 100 tokens this minute: must wait
    assert clock.slept and sum(clock.slept) >= 59
    ledger.record("m", 40, first)  # settling replaces the estimate, no double count
    assert sum(t for _, t in ledger._minute["m"]) == 100


def test_single_call_over_tpm_refused() -> None:
    with pytest.raises(BudgetExceededError, match="TPM"):
        _ledger(tpm=50).reserve("m", 51)


def test_unlimited_model() -> None:
    clock = FakeClock()
    ledger = UsageLedger(":memory:", {"m": ModelLimits(family="f")}, 0.9, 0, clock, clock.sleep)
    for _ in range(50):
        ledger.reserve("m", 10**6)
        ledger.record("m", 10**6)
    assert clock.slept == []


def _req(text: str = "hi", **kw: Any) -> ChatRequest:
    return ChatRequest(
        model="m", messages=(Message(role="user", content=text),), max_tokens=10, **kw
    )


def test_cache_key_is_exact_match_over_everything() -> None:
    base = cache_key("groq", _req())
    assert base == cache_key("groq", _req())
    assert base != cache_key("google", _req())
    assert base != cache_key("groq", _req("hi "))  # near-identical text is a different key
    assert base != cache_key("groq", _req(temperature=0.5))
    assert base != cache_key("groq", _req(json_mode=False))


def test_cache_roundtrip_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite3"
    cache = LLMCache(path)
    assert cache.get("k") is None
    cache.put("k", ChatResult(text="x", prompt_tokens=3, completion_tokens=2, latency_s=1.0))
    again = LLMCache(path).get("k")
    assert again is not None and again.text == "x" and again.total_tokens == 5
    assert again.latency_s == 0 and len(LLMCache(path)) == 1


def test_input_only_minute_limit_settles_with_prompt_tokens() -> None:
    clock = FakeClock()
    ledger = _ledger(clock=clock, rpm=100, rpd=100, tpd=10**6, tpm=100)
    b = ledger.reserve("m", 30)
    ledger.record("m", 500, b, minute_tokens=30)  # 500 in total today, 30 count this minute
    assert ledger.today("m") == (1, 500) and sum(t for _, t in ledger._minute["m"]) == 30
    ledger.reserve("m", 60)  # fits: 30 + 60 <= 100
    assert clock.slept == []
