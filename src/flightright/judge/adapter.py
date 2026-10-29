"""Adapter: our LLM client (fallback chains, ledger, cache) as JudgeKit's `StructuredLLM`."""

from __future__ import annotations

from typing import TypeVar

from judgekit import CallInfo, LLMCallError, StructuredResult
from pydantic import BaseModel

from flightright.llm.client import LLMClient
from flightright.llm.types import LLMUnavailableError

T = TypeVar("T", bound=BaseModel)


class ClientJudgeLLM:
    """One judge role (e.g. `judge_a`) of our client, speaking JudgeKit's protocol."""

    def __init__(self, client: LLMClient, role: str, use_cache: bool = True) -> None:
        self.client = client
        self.role = role
        self.use_cache = use_cache

    def complete(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        avoid_families: frozenset[str] = frozenset(),
    ) -> StructuredResult[T]:
        try:
            res = self.client.structured(
                self.role,
                system,
                user,
                schema,
                exclude_families=avoid_families,
                use_cache=self.use_cache,
            )
        except LLMUnavailableError as exc:
            raise LLMCallError(str(exc)) from exc
        m = res.meta
        info = CallInfo(
            model=m.model,
            family=m.family,
            input_tokens=m.prompt_tokens,
            output_tokens=m.completion_tokens,
            latency_s=m.latency_s,
            attempts=1 + m.validation_retries,
            cached=m.cached,
        )
        return StructuredResult(res.value, (info,))
