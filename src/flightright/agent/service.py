"""Conversation service (FR-1): the entry point the API and n8n call.

service = ConversationService.build(...)
cid = service.create_conversation(channel="telegram", booking_ref="ABC123")
service.handle_message(cid, "My flight is cancelled", idempotency_key="tg-1001")
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from flightright.agent.graph import Deps, build_graph


class MessageOutcome(BaseModel):
    """What the customer API returns: the final reply only, never drafts or scores (SPEC §7.1)."""

    reply_text: str
    status: Literal["answered", "handed_off"]
    ticket_id: str | None = None
    duplicate: bool = False


class ConversationService:
    def __init__(self, deps: Deps, checkpoint_path: Path | str = ":memory:") -> None:
        if str(checkpoint_path) != ":memory:":
            Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        self.saver = SqliteSaver(self._conn)
        self.deps = deps
        self.graph = build_graph(deps, self.saver)

    @staticmethod
    def _cfg(conversation_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": conversation_id}}

    def create_conversation(
        self, channel: str, passenger_ref: str | None = None, booking_ref: str | None = None
    ) -> str:
        cid = "C-" + uuid.uuid4().hex[:12]
        self.graph.invoke(
            {
                "conversation_id": cid,
                "channel": channel,
                "passenger_ref": passenger_ref,
                "booking_ref": booking_ref.upper() if booking_ref else None,
                "incoming": None,
                "status": "active",
                "transcript": [],
                "processed": {},
                "events": [],
                "abuse_count": 0,
                "turn": 0,
                "ticket": None,
            },
            self._cfg(cid),
        )
        return cid

    def handle_message(
        self,
        conversation_id: str,
        text: str,
        idempotency_key: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> MessageOutcome:
        """Run one turn. `on_event` receives each pipeline event as soon as its node finishes
        (triage.completed, draft.created, judgement.completed, ...), for live views (SSE)."""
        config = self._cfg(conversation_id)
        state = self.graph.get_state(config).values
        if not state:
            raise KeyError(f"unknown conversation {conversation_id}")
        done = state.get("processed", {}).get(idempotency_key)
        if done is not None:  # FR-1.4: duplicate delivery -> same reply, no new actions
            return MessageOutcome(**done, duplicate=True)
        seen = len(state.get("events", []))
        incoming = {"incoming": {"text": text, "idempotency_key": idempotency_key}}
        for update in self.graph.stream(incoming, config, stream_mode="updates"):
            for node_update in update.values():
                events = (
                    (node_update or {}).get("events") if isinstance(node_update, dict) else None
                )
                if events and len(events) > seen:
                    if on_event is not None:
                        for event in events[seen:]:
                            on_event(event)
                    seen = len(events)
        outcome = self.graph.get_state(config).values["outcome"]
        return MessageOutcome(**outcome)

    def state(self, conversation_id: str) -> dict[str, Any]:
        """Full internal state (ops API only; never returned to passengers)."""
        return dict(self.graph.get_state(self._cfg(conversation_id)).values)
