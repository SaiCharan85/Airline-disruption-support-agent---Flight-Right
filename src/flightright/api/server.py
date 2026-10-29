"""Run the API: `uv run python -m flightright.api.server` (http://127.0.0.1:8000, docs at /docs).

Environment (.env): OPS_API_KEY, OPS_ADMIN_KEY, provider keys, optional N8N_EVENTS_WEBHOOK_URL and
N8N_HMAC_SECRET. FLIGHTRIGHT_JUDGE=placeholder runs without the LLM judge (code checks only).
FLIGHTRIGHT_HOST / FLIGHTRIGHT_PORT override the bind address (0.0.0.0 inside Docker).
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from flightright.agent.judging import CodeChecksOnlyJudge, Judge
from flightright.agent.live import live_engine
from flightright.api.app import create_app
from flightright.api.runtime import Runtime
from flightright.config import load_config, load_env
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient


def build() -> FastAPI:
    load_env()
    cfg = load_config()
    client = LLMClient.from_config(cfg.models)

    def judge_factory(rt: Runtime) -> Judge:
        if os.environ.get("FLIGHTRIGHT_JUDGE") == "placeholder":
            return CodeChecksOnlyJudge()
        return SeverityAwareJudge.for_client(client, exemplars=rt.exemplars)

    runtime = Runtime(cfg, client, live_engine(), judge_factory)
    api_key, admin_key = os.environ.get("OPS_API_KEY", ""), os.environ.get("OPS_ADMIN_KEY", "")
    if not api_key or not admin_key:
        raise SystemExit("OPS_API_KEY and OPS_ADMIN_KEY must be set (see .env.example)")
    return create_app(runtime, api_key, admin_key)


def main() -> None:
    uvicorn.run(
        build(),
        host=os.environ.get("FLIGHTRIGHT_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLIGHTRIGHT_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
