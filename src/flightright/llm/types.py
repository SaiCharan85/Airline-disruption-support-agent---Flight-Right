"""Shared LLM types and errors (provider-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    role: Role
    content: str


@dataclass(frozen=True)
class ChatRequest:
    model: str  # provider's model id, e.g. "openai/gpt-oss-120b"
    messages: tuple[Message, ...]
    max_tokens: int
    temperature: float = 0.0
    reasoning_effort: str | None = None
    json_mode: bool = True


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMError(Exception):
    """Base for every LLM-layer failure."""


class ProviderError(LLMError):
    """The provider answered with an error, or could not be reached."""


class TransientProviderError(ProviderError):
    """A server error (5xx) or network failure that usually clears within seconds."""


class RateLimitedError(ProviderError):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class BudgetExceededError(LLMError):
    """The usage ledger refuses the call: it would cross the free-tier safety limit."""


class LLMUnavailableError(LLMError):
    """Every model in the role's chain failed or is out of budget: route to a human."""
