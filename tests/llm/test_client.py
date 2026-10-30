from typing import Any

import pytest
from pydantic import BaseModel

from flightright.config import load_config
from flightright.llm.client import LLMClient, Result, parse_json, parse_text, system_prompt
from flightright.llm.testing import scripted_client
from flightright.llm.types import (
    LLMUnavailableError,
    ProviderError,
    RateLimitedError,
    TransientProviderError,
)


class Out(BaseModel):
    label: str
    score: float


GOOD = '{"label": "ok", "score": 0.5}'


def _call(client: LLMClient, user: str = "case", **kw: Any) -> Result[Out]:
    return client.structured("agent", "Decide.", user, Out, **kw)


def test_happy_path_meta_and_request() -> None:
    client, prov, _ = scripted_client([GOOD])
    res = _call(client)
    assert res.value == Out(label="ok", score=0.5)
    assert res.meta.model == "a:m1" and res.meta.family == "fam1"
    assert res.meta.prompt_tokens == 100 and res.meta.cost_usd == 0.0 and not res.meta.cached
    req = prov["a"].requests[0]
    assert req.model == "m1" and req.reasoning_effort == "low" and req.max_tokens == 50
    assert (
        req.json_mode and req.messages[0].role == "system" and '"label"' in req.messages[0].content
    )


@pytest.mark.parametrize(("otpm", "sent"), [(40, 40), (100, 50)])
def test_max_tokens_stays_under_output_per_minute_limit(otpm: int, sent: int) -> None:
    client, prov, _ = scripted_client([GOOD], otpm=otpm)
    _call(client)
    assert prov["a"].requests[0].max_tokens == sent


@pytest.mark.parametrize(("otpm", "sent"), [(None, 80), (70, 70)])
def test_thinking_models_get_allowance_on_top_still_under_otpm(otpm: int | None, sent: int) -> None:
    client, prov, _ = scripted_client([GOOD], thinking_tokens=30, otpm=otpm)
    _call(client)
    assert prov["a"].requests[0].max_tokens == sent


def test_second_identical_call_is_free() -> None:
    client, prov, _ = scripted_client([GOOD])
    _call(client)
    again = _call(client)
    assert again.meta.cached and len(prov["a"].requests) == 1
    assert client.ledger.today("a:m1") == (1, 120)


def test_validation_retry_once_with_error() -> None:
    client, prov, _ = scripted_client(['{"label": "ok"}', GOOD])
    res = _call(client)
    assert res.value.score == 0.5 and res.meta.validation_retries == 1
    retry = prov["a"].requests[1].messages
    assert retry[-2].role == "assistant" and "invalid" in retry[-1].content


def test_falls_back_after_two_invalid_outputs() -> None:
    client, prov, _ = scripted_client(["nope", "still nope"], [GOOD])
    res = _call(client)
    assert res.meta.model == "b:m2"
    assert prov["b"].requests[0].reasoning_effort is None  # m2 does not accept efforts


def test_provider_error_falls_back() -> None:
    client, _, _ = scripted_client([ProviderError("down")], [GOOD])
    res = _call(client)
    assert res.meta.model == "b:m2" and "down" in res.meta.errors[0]


def test_budget_exhausted_falls_back_then_unavailable() -> None:
    client, _, _ = scripted_client([GOOD, GOOD], [GOOD], rpd=1)
    assert _call(client, "c1").meta.model == "a:m1"
    assert _call(client, "c2").meta.model == "b:m2"  # a is out of daily budget
    with pytest.raises(LLMUnavailableError, match="daily safety limit"):
        _call(client, "c3")


def test_short_rate_limit_waits_and_retries_same_model() -> None:
    client, prov, _ = scripted_client([RateLimitedError("429", retry_after_s=5), GOOD])
    assert _call(client).meta.model == "a:m1"
    assert len(prov["a"].requests) == 2


def test_repeated_short_rate_limits_are_waited_out_then_fall_back() -> None:
    limited = [RateLimitedError("429", retry_after_s=5)]
    client, prov, _ = scripted_client(limited * 3 + [GOOD])
    assert _call(client).meta.model == "a:m1" and len(prov["a"].requests) == 4
    client, prov, _ = scripted_client(limited * 4, [GOOD])
    assert _call(client).meta.model == "b:m2" and len(prov["a"].requests) == 4


def test_long_rate_limit_moves_on() -> None:
    client, _, _ = scripted_client([RateLimitedError("429", retry_after_s=600)], [GOOD])
    assert _call(client).meta.model == "b:m2"


def test_FR_8_8_excluded_family_and_missing_provider_are_skipped() -> None:
    client, _, _ = scripted_client([GOOD])  # provider b not configured
    with pytest.raises(LLMUnavailableError) as exc:
        _call(client, exclude_families=frozenset({"fam1"}))
    assert "excluded" in str(exc.value) and "not configured" in str(exc.value)
    assert client.family_of("b:m2") == "fam2"


def test_parse_json_strips_fences_and_system_prompt_has_schema() -> None:
    assert parse_json(f"```json\n{GOOD}\n```", Out).label == "ok"
    sp = system_prompt("Be brief.", Out)
    assert sp.startswith("Be brief.") and '"score"' in sp


def test_estimate_tokens_includes_max_completion() -> None:
    client, _, _ = scripted_client([])
    assert client.estimate_tokens("agent", "x" * 40, "") == 11 + 50


def test_pinned_chain_never_falls_back() -> None:
    client, prov, _ = scripted_client([ProviderError("down")], [GOOD])
    with pytest.raises(LLMUnavailableError):
        _call(client, chain=["a:m1"])
    assert prov["b"].requests == []
    with pytest.raises(KeyError):
        _call(client, chain=["a:unknown"])


def test_parse_json_drops_inline_thought_block_with_json_drafts() -> None:
    reply = '<thought>* draft: `{"label": "draft"}`\n* final below</thought>' + GOOD
    assert parse_json(reply, Out).label == "ok"


def test_transient_server_error_retries_same_model_with_backoff() -> None:
    err = TransientProviderError("HTTP 500")
    client, prov, clock = scripted_client([err, err, GOOD])
    res = _call(client)
    assert res.meta.model == "a:m1" and len(prov["a"].requests) == 3
    assert clock.slept == [5.0, 15.0]


def test_transient_errors_past_the_retries_fall_back() -> None:
    errs: list[Any] = [TransientProviderError("500")] * 3
    client, prov, _ = scripted_client(errs, [GOOD])
    assert _call(client).meta.model == "b:m2" and len(prov["a"].requests) == 3


# ------------------------------------------------------------------ free-text calls


def test_text_call_is_not_json_mode_and_strips_thoughts() -> None:
    client, prov, _ = scripted_client(["<thought>plan</thought>Dear passenger, ..."])
    res = client.text("agent", "Draft a reply.", "case")
    assert res.value == "Dear passenger, ..." and not prov["a"].requests[0].json_mode


def test_text_call_retries_empty_output_then_falls_back() -> None:
    client, _, _ = scripted_client(["", "<thought>only thinking</thought>"], ["Reply."])
    res = client.text("agent", "Draft a reply.", "case")
    assert res.value == "Reply." and res.meta.model == "b:m2"


def test_parse_text_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_text("   ")


def test_from_config_skips_providers_without_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    cfg = load_config().models.model_copy(
        update={
            "cache_path": str(tmp_path / "c.sqlite3"),
            "usage_path": str(tmp_path / "u.sqlite3"),
        }
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client = LLMClient.from_config(cfg)
    assert set(client.providers) == {"google", "ollama"}
