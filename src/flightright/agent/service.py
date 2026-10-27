"""Conversation service (FR-1): the entry point the API and n8n call.

service = ConversationService.build(...)
cid = service.create_conversation(channel="telegram", booking_ref="ABC123")
service.handle_message(cid, "My flight is cancelled", idempotency_key="tg-1001")
"""

from __future__ import annotations

import sqlite3
import uuid
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
        self, conversation_id: str, text: str, idempotency_key: str
    ) -> MessageOutcome:
        state = self.graph.get_state(self._cfg(conversation_id)).values
        if not state:
            raise KeyError(f"unknown conversation {conversation_id}")
        done = state.get("processed", {}).get(idempotency_key)
        if done is not None:  # FR-1.4: duplicate delivery -> same reply, no new actions
            return MessageOutcome(**done, duplicate=True)
        result = self.graph.invoke(
            {"incoming": {"text": text, "idempotency_key": idempotency_key}},
            self._cfg(conversation_id),
        )
        return MessageOutcome(**result["outcome"])

    def state(self, conversation_id: str) -> dict[str, Any]:
        """Full internal state (ops API only; never returned to passengers)."""
        return dict(self.graph.get_state(self._cfg(conversation_id)).values)
