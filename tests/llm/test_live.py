"""One tiny live call per judge/agent model family (never in CI: `-m live_llm` only).

Run:  uv run pytest -m live_llm tests/llm/test_live.py
Costs 3 small free-tier requests; repeated runs are served from the on-disk cache.
"""

import os
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from flightright.config import ROOT, load_config
from flightright.llm.client import LLMClient

pytestmark = pytest.mark.live_llm


class Ping(BaseModel):
    answer: str


def _load_env(path: Path) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"([A-Z_]+)=(.+)$", line.strip())
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2)


@pytest.mark.parametrize(
    "model", ["groq:openai/gpt-oss-120b", "google:gemma-4-26b-a4b-it", "groq:qwen/qwen3.8-27b"]
)
def test_live_structured_call(model: str) -> None:
    _load_env(ROOT / ".env")
    client = LLMClient.from_config(load_config().models)
    res = client.structured(
        "agent", "Answer in one word.", "What is the capital of Portugal?", Ping, chain=[model]
    )
    assert "lisbon" in res.value.answer.lower()
    assert res.meta.model == model and res.meta.cost_usd == 0.0
