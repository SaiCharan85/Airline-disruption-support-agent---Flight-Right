"""Exact-match on-disk LLM cache (SQLite). Re-running evals never re-spends quota.

Exact match only: the key hashes provider + model + messages + every generation parameter.
Decision-path calls must never be answered from a *similar* past call, so there is deliberately
no semantic cache here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from flightright.llm.providers import request_body
from flightright.llm.types import ChatRequest, ChatResult


def cache_key(provider: str, request: ChatRequest) -> str:
    payload = json.dumps(
        {"provider": provider, **request_body(request)}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # graph nodes may run in parallel threads: one shared connection, serialized by a lock
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, text TEXT NOT NULL,"
            " prompt_tokens INTEGER, completion_tokens INTEGER)"
        )

    def get(self, key: str) -> ChatResult | None:
        with self._lock:
            row = self._db.execute(
                "SELECT text, prompt_tokens, completion_tokens FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return ChatResult(text=row[0], prompt_tokens=row[1], completion_tokens=row[2], latency_s=0)

    def put(self, key: str, result: ChatResult) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                (key, result.text, result.prompt_tokens, result.completion_tokens),
            )

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0])
