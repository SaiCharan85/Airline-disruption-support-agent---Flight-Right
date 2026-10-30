"""Persistent per-model usage ledger: hard stop below every free-tier limit.

Daily counters (requests, tokens) live in SQLite so they survive restarts and are shared by
every script. Per-minute limits are enforced by waiting (a sliding 60 s window in memory).
Crossing a daily limit raises BudgetExceededError; the client then falls back to the next model.
It never retries the same model silently.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flightright.config import ModelLimits
from flightright.llm.types import BudgetExceededError


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish estimate (~4 characters per token for English/JSON)."""
    return len(text) // 4 + 1


class UsageLedger:
    def __init__(
        self,
        path: Path | str,
        catalog: dict[str, ModelLimits],
        safety_margin: float,
        day_reset_utc_offset_hours: int,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS usage (day TEXT, model TEXT, requests INTEGER,"
            " tokens INTEGER, PRIMARY KEY (day, model))"
        )
        self.catalog = catalog
        self.margin = safety_margin
        self.offset = timedelta(hours=day_reset_utc_offset_hours)
        self.clock = clock
        self.sleep = sleep
        # Per-minute window: [start time, tokens] per call. A call books its estimate when it is
        # admitted, so parallel callers see each other before any of them finishes.
        self._minute: dict[str, deque[list[float]]] = {}

    def day(self) -> str:
        return (datetime.fromtimestamp(self.clock(), UTC) + self.offset).date().isoformat()

    def today(self, model: str) -> tuple[int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT requests, tokens FROM usage WHERE day = ? AND model = ?",
                (self.day(), model),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _cap(self, limit: int | None) -> float:
        return float("inf") if limit is None else limit * self.margin

    def headroom(self, model: str) -> tuple[float, float]:
        """Requests and tokens still allowed today for this model."""
        lim = self.catalog[model]
        requests, tokens = self.today(model)
        return self._cap(lim.rpd) - requests, self._cap(lim.tpd) - tokens

    def reserve(self, model: str, est_tokens: int) -> list[float]:
        """Refuse (daily) or wait (per-minute) before a call of about `est_tokens`; returns the
        booking that `record` later settles with the actual token count.

        Checks and booking run under the lock; waiting happens outside it, so a model that is
        waiting on its per-minute limit never blocks calls to other models.
        """
        while True:
            with self._lock:
                wait = self._check(model, est_tokens)
                if wait <= 0:
                    booking = [self.clock(), float(est_tokens)]
                    self._minute.setdefault(model, deque()).append(booking)
                    return booking
            self.sleep(wait)

    def _check(self, model: str, est_tokens: int) -> float:
        """Raise if today's budget is spent; else seconds to wait for the minute window (0 = go)."""
        req_left, tok_left = self.headroom(model)
        if req_left < 1 or tok_left < est_tokens:
            raise BudgetExceededError(
                f"{model}: daily safety limit reached ({req_left:.0f} requests, "
                f"{tok_left:.0f} tokens left; need ~{est_tokens})"
            )
        lim = self.catalog[model]
        rpm, tpm = self._cap(lim.rpm), self._cap(lim.tpm)
        if est_tokens > tpm:
            raise BudgetExceededError(
                f"{model}: one call (~{est_tokens} tokens) exceeds the TPM cap"
            )
        window = self._minute.setdefault(model, deque())
        now = self.clock()
        while window and now - window[0][0] >= 60:
            window.popleft()
        used = sum(t for _, t in window)  # settled calls plus bookings still in flight
        if len(window) + 1 <= rpm and used + est_tokens <= tpm:
            return 0.0
        return max(0.5, 60 - (now - window[0][0]))

    def record(
        self,
        model: str,
        tokens: int,
        booking: list[float] | None = None,
        minute_tokens: int | None = None,
    ) -> None:
        """Count a finished call. `minute_tokens` is what the per-minute limit counts when that
        differs from the daily total (input-only per-minute limits)."""
        minute = float(tokens if minute_tokens is None else minute_tokens)
        with self._lock, self._db:
            window = self._minute.setdefault(model, deque())
            if booking is not None and any(b is booking for b in window):
                booking[1] = minute  # settle the estimate with the real count
            else:
                window.append([self.clock(), minute])
            self._db.execute(
                "INSERT INTO usage VALUES (?, ?, 1, ?) ON CONFLICT (day, model) DO UPDATE SET"
                " requests = requests + 1, tokens = tokens + excluded.tokens",
                (self.day(), model, tokens),
            )
