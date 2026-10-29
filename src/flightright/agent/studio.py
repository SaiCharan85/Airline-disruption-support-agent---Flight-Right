"""Graph entry point for LangGraph Studio (`langgraph.json`), with live models."""

from __future__ import annotations

from typing import Any

from flightright.agent.graph import Deps, build_graph
from flightright.agent.live import live_engine
from flightright.config import load_config, load_env
from flightright.judge import SeverityAwareJudge
from flightright.llm.client import LLMClient
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.triage.triage import Triage


def make_graph() -> Any:
    load_env()
    cfg = load_config()
    client = LLMClient.from_config(cfg.models)
    airline = MockAirline(cfg.authority, ActionStore(), seed=cfg.seed)
    tools = AgentTools(airline, HistoryStore(), Playbooks())
    deps = Deps(
        config=cfg,
        engine=live_engine(),
        triage=Triage(client),
        client=client,
        tools=tools,
        judge=SeverityAwareJudge.for_client(client),
    )
    return build_graph(deps)  # Studio supplies its own checkpointer
