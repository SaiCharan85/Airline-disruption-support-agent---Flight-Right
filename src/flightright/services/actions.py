"""Idempotent action log (NFR-4): a repeated idempotency key returns the original result.

Every side-effecting service call (voucher, refund, ticket) goes through `ActionStore.once`.
Duplicate deliveries (e.g. n8n retries) therefore never issue a second voucher or refund.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from flightright.services.types import IdempotencyConflictError

M = TypeVar("M", bound=BaseModel)


def _hash(params: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


class ActionStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS actions (kind TEXT, idem_key TEXT, params_hash TEXT,"
            " conversation_id TEXT, amount_eur INTEGER, result TEXT, PRIMARY KEY (kind, idem_key))"
        )

    def once(
        self,
        kind: str,
        idem_key: str,
        params: dict[str, object],
        model: type[M],
        create: Callable[[], M],
        conversation_id: str | None = None,
        amount_eur: int = 0,
    ) -> tuple[M, bool]:
        """Run `create` at most once per (kind, key). Returns (result, created_now)."""
        digest = _hash(params)
        with self._lock:
            row = self._db.execute(
                "SELECT params_hash, result FROM actions WHERE kind = ? AND idem_key = ?",
                (kind, idem_key),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise IdempotencyConflictError(
                        f"{kind}: idempotency key {idem_key!r} reused with different parameters"
                    )
                return model.model_validate_json(row[1]), False
            result = create()
            with self._db:
                self._db.execute(
                    "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, idem_key, digest, conversation_id, amount_eur, result.model_dump_json()),
                )
            return result, True

    def total_eur(self, kind: str, conversation_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(amount_eur), 0) FROM actions"
                " WHERE kind = ? AND conversation_id = ?",
                (kind, conversation_id),
            ).fetchone()
        return int(row[0])

    def count(self, kind: str) -> int:
        with self._lock:
            return int(
                self._db.execute("SELECT COUNT(*) FROM actions WHERE kind = ?", (kind,)).fetchone()[
                    0
                ]
            )
