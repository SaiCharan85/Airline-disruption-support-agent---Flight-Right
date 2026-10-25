"""Passenger history store (FR-10.2 episodic, FR-10.3 preferences), keyed by passenger.

Holds prior cases, contacts and human resolutions (FR-9.5) so the agent can acknowledge repeat
contact and the judge can score context awareness. Lookups are by key only (no retrieval).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

REPEAT_CONTACT_WINDOW = timedelta(days=14)


class PriorCase(BaseModel):
    case_id: str
    booking_ref: str
    disruption_type: str
    summary: str
    status: Literal["open", "resolved"]
    opened_at: datetime


class Contact(BaseModel):
    conversation_id: str
    booking_ref: str
    channel: str
    at: datetime


class Resolution(BaseModel):
    case_id: str
    decision: str
    notes: str
    compensation_paid_eur: int | None = None
    resolved_at: datetime


class PassengerHistory(BaseModel):
    passenger_ref: str
    cases: list[PriorCase] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    resolutions: list[Resolution] = Field(default_factory=list)
    preferences: dict[str, str] = Field(default_factory=dict)

    def repeat_contacts(self, booking_ref: str, now: datetime) -> int:
        """Earlier contacts about the same booking within the repeat-contact window (FR-3.1)."""
        return sum(
            1
            for c in self.contacts
            if c.booking_ref == booking_ref and now - c.at <= REPEAT_CONTACT_WINDOW
        )


class HistoryStore:
    def __init__(
        self,
        path: Path | str = ":memory:",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self.clock = clock
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, passenger_ref TEXT,
                    booking_ref TEXT, disruption_type TEXT, summary TEXT, status TEXT,
                    opened_at TEXT);
                CREATE TABLE IF NOT EXISTS contacts (passenger_ref TEXT, conversation_id TEXT,
                    booking_ref TEXT, channel TEXT, at TEXT,
                    PRIMARY KEY (passenger_ref, conversation_id));
                CREATE TABLE IF NOT EXISTS resolutions (case_id TEXT PRIMARY KEY, decision TEXT,
                    notes TEXT, compensation_paid_eur INTEGER, resolved_at TEXT);
                CREATE TABLE IF NOT EXISTS preferences (passenger_ref TEXT, key TEXT, value TEXT,
                    PRIMARY KEY (passenger_ref, key));
                """
            )

    def add_case(self, passenger_ref: str, case: PriorCase) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    case.case_id,
                    passenger_ref,
                    case.booking_ref,
                    case.disruption_type,
                    case.summary,
                    case.status,
                    case.opened_at.isoformat(),
                ),
            )

    def record_contact(
        self, passenger_ref: str, conversation_id: str, booking_ref: str, channel: str
    ) -> None:
        """Idempotent per conversation: the first contact time is kept."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO contacts VALUES (?, ?, ?, ?, ?)",
                (passenger_ref, conversation_id, booking_ref, channel, self.clock().isoformat()),
            )

    def add_resolution(self, resolution: Resolution) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO resolutions VALUES (?, ?, ?, ?, ?)",
                (
                    resolution.case_id,
                    resolution.decision,
                    resolution.notes,
                    resolution.compensation_paid_eur,
                    resolution.resolved_at.isoformat(),
                ),
            )
            self._db.execute(
                "UPDATE cases SET status = 'resolved' WHERE case_id = ?", (resolution.case_id,)
            )

    def set_preference(self, passenger_ref: str, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO preferences VALUES (?, ?, ?)", (passenger_ref, key, value)
            )

    def get(self, passenger_ref: str) -> PassengerHistory:
        with self._lock:
            cases = [
                PriorCase(
                    case_id=r[0],
                    booking_ref=r[1],
                    disruption_type=r[2],
                    summary=r[3],
                    status=r[4],
                    opened_at=datetime.fromisoformat(r[5]),
                )
                for r in self._db.execute(
                    "SELECT case_id, booking_ref, disruption_type, summary, status, opened_at"
                    " FROM cases"
                    " WHERE passenger_ref = ? ORDER BY opened_at",
                    (passenger_ref,),
                )
            ]
            contacts = [
                Contact(
                    conversation_id=r[0],
                    booking_ref=r[1],
                    channel=r[2],
                    at=datetime.fromisoformat(r[3]),
                )
                for r in self._db.execute(
                    "SELECT conversation_id, booking_ref, channel, at FROM contacts"
                    " WHERE passenger_ref = ?"
                    " ORDER BY at",
                    (passenger_ref,),
                )
            ]
            resolutions = [
                Resolution(
                    case_id=r[0],
                    decision=r[1],
                    notes=r[2],
                    compensation_paid_eur=r[3],
                    resolved_at=datetime.fromisoformat(r[4]),
                )
                for r in self._db.execute(
                    "SELECT r.case_id, r.decision, r.notes, r.compensation_paid_eur, r.resolved_at"
                    " FROM resolutions r JOIN cases c ON c.case_id = r.case_id"
                    " WHERE c.passenger_ref = ?"
                    " ORDER BY r.resolved_at",
                    (passenger_ref,),
                )
            ]
            prefs = dict(
                self._db.execute(
                    "SELECT key, value FROM preferences WHERE passenger_ref = ?", (passenger_ref,)
                ).fetchall()
            )
        return PassengerHistory(
            passenger_ref=passenger_ref,
            cases=cases,
            contacts=contacts,
            resolutions=resolutions,
            preferences=prefs,
        )
