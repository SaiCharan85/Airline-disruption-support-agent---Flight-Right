"""One adapter for every provider: Groq, Google AI Studio and Ollama all serve the
OpenAI-compatible `POST {base_url}/chat/completions` endpoint, so no vendor SDKs are needed."""

from __future__ import annotations

import os
import time
from typing import Any, Protocol

import httpx

from flightright.llm.types import (
    ChatRequest,
    ChatResult,
    ProviderError,
    RateLimitedError,
    TransientProviderError,
)


class ChatProvider(Protocol):
    name: str

    def chat(self, request: ChatRequest) -> ChatResult: ...


def request_body(request: ChatRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "messages": [m.model_dump() for m in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.reasoning_effort:
        body["reasoning_effort"] = request.reasoning_effort
    if request.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,  # tests inject httpx.MockTransport
    ) -> None:
        self.name = name
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    @classmethod
    def from_env(
        cls, name: str, base_url: str, api_key_env: str | None, timeout_s: float
    ) -> OpenAICompatProvider:
        key = os.environ.get(api_key_env) if api_key_env else None
        if api_key_env and not key:
            raise ProviderError(f"{name}: {api_key_env} is not set (see .env.example)")
        return cls(name, base_url, key, timeout_s)

    def chat(self, request: ChatRequest) -> ChatResult:
        start = time.perf_counter()
        try:
            response = self._http.post("chat/completions", json=request_body(request))
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 429:
            # the 429 body names the exact quota hit (Gemini: quotaMetric + quotaValue): keep it
            raise RateLimitedError(
                f"{self.name}: rate limited: {response.text[:400]}", _retry_after(response)
            )
        if response.status_code >= 500:
            raise TransientProviderError(
                f"{self.name}: HTTP {response.status_code}: {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: malformed response: {response.text[:300]}") from exc
        return ChatResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_s=time.perf_counter() - start,
        )
