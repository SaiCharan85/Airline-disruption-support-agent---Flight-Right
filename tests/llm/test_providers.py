import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from flightright.llm.providers import OpenAICompatProvider, request_body
from flightright.llm.types import (
    ChatRequest,
    Message,
    ProviderError,
    RateLimitedError,
    TransientProviderError,
)

REQ = ChatRequest(
    model="m", messages=(Message(role="user", content="hi"),), max_tokens=10, reasoning_effort="low"
)


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatProvider:
    return OpenAICompatProvider("p", "http://x/v1", "k", 5, transport=httpx.MockTransport(handler))


def test_request_body_shape() -> None:
    body = request_body(REQ)
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low" and body["temperature"] == 0.0
    plain = request_body(ChatRequest(model="m", messages=(), max_tokens=1, json_mode=False))
    assert "response_format" not in plain and "reasoning_effort" not in plain


def test_chat_parses_text_and_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"a": 1}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    result = _provider(handler).chat(REQ)
    assert result.text == '{"a": 1}' and result.total_tokens == 10
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["auth"] == "Bearer k" and seen["body"]["model"] == "m"


def test_429_carries_retry_after() -> None:
    p = _provider(lambda r: httpx.Response(429, headers={"retry-after": "7"}))
    with pytest.raises(RateLimitedError) as exc:
        p.chat(REQ)
    assert exc.value.retry_after_s == 7.0


def test_429_with_unparseable_retry_after() -> None:
    p = _provider(lambda r: httpx.Response(429, headers={"retry-after": "soon"}))
    with pytest.raises(RateLimitedError) as exc:
        p.chat(REQ)
    assert exc.value.retry_after_s is None


@pytest.mark.parametrize(
    "response", [httpx.Response(500, text="boom"), httpx.Response(200, json={"choices": []})]
)
def test_http_and_shape_errors(response: httpx.Response) -> None:
    with pytest.raises(ProviderError):
        _provider(lambda r: response).chat(REQ)


def test_network_error_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="ConnectError"):
        _provider(handler).chat(REQ)


@pytest.mark.parametrize(("status", "transient"), [(500, True), (503, True), (400, False)])
def test_server_errors_are_transient_client_errors_are_not(status: int, transient: bool) -> None:
    with pytest.raises(ProviderError) as exc:
        _provider(lambda r: httpx.Response(status, text="x")).chat(REQ)
    assert isinstance(exc.value, TransientProviderError) is transient


def test_missing_key_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(ProviderError, match="NOPE_KEY"):
        OpenAICompatProvider.from_env("p", "http://x", "NOPE_KEY", 5)
    assert OpenAICompatProvider.from_env("ollama", "http://x", None, 5).name == "ollama"
