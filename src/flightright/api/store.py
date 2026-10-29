"""Ops store (FR-11): every draft attempt, check result, judgement, handoff and event, queryable.

The conversation graph keeps one turn of detail in its checkpointed state; after each turn the API
copies that turn here, so ops queries, metrics, the Judge Monitor and the Workbench work across all
conversations and all turns. SQLite, one file, thread-safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, channel TEXT, booking_ref TEXT,
    passenger_ref TEXT, status TEXT, tier TEXT, turns INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (conversation_id TEXT, seq INTEGER, role TEXT, text TEXT, at TEXT,
    PRIMARY KEY (conversation_id, seq));
CREATE TABLE IF NOT EXISTS attempts (attempt_id TEXT PRIMARY KEY, conversation_id TEXT, turn INTEGER,
    attempt_no INTEGER, draft TEXT, model TEXT, checks TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS judgements (judgement_id TEXT PRIMARY KEY, conversation_id TEXT, turn INTEGER,
    attempt_id TEXT, target TEXT, tier TEXT, verdict TEXT, decision TEXT, reason TEXT, record TEXT,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS handoffs (ticket_id TEXT PRIMARY KEY, conversation_id TEXT, tier TEXT,
    urgency TEXT, status TEXT, reason TEXT, sla_due_at TEXT, record TEXT, resolution TEXT,
    acknowledged_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, seq INTEGER,
    event TEXT, payload TEXT, ts TEXT, UNIQUE (conversation_id, seq));
CREATE TABLE IF NOT EXISTS corrections (correction_id TEXT PRIMARY KEY, judgement_id TEXT, split TEXT,
    record TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS examples (example_id TEXT PRIMARY KEY, record TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, dimension TEXT, change TEXT, status TEXT,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ticket_id TEXT,
    channel TEXT, text TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS j_created ON judgements (created_at);
CREATE INDEX IF NOT EXISTS j_conv ON judgements (conversation_id);
"""


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


class OpsStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._db:
            self._db.executescript(SCHEMA)

    # ---------------------------------------------------------------- writes

    def upsert_conversation(
        self,
        cid: str,
        *,
        channel: str,
        booking_ref: str | None,
        passenger_ref: str | None,
        status: str,
        tier: str | None,
        turns: int,
        now: datetime,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "booking_ref = excluded.booking_ref, passenger_ref = excluded.passenger_ref, "
                "status = excluded.status, tier = COALESCE(excluded.tier, tier), turns = excluded.turns, "
                "updated_at = excluded.updated_at",
                (
                    cid,
                    channel,
                    booking_ref,
                    passenger_ref,
                    status,
                    tier,
                    turns,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def record_turn(self, state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        """Copy one finished turn from graph state. Returns the events not stored before."""
        cid = state["conversation_id"]
        turn = int(state.get("turn", 0))
        triage = state.get("triage") or {}
        booking = state.get("booking") or {}
        self.upsert_conversation(
            cid,
            channel=state.get("channel", "webhook"),
            booking_ref=state.get("booking_ref") or booking.get("booking_ref"),
            passenger_ref=state.get("passenger_ref") or booking.get("passenger_ref"),
            status=state.get("status", "active"),
            tier=triage.get("tier"),
            turns=turn,
            now=now,
        )
        with self._lock, self._db:
            for seq, m in enumerate(state.get("transcript", [])):
                self._db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?)",
                    (cid, seq, m["role"], m["text"], m.get("at", now.isoformat())),
                )
            attempts = state.get("attempts") or []
            for a in attempts:
                self._db.execute(
                    "INSERT OR REPLACE INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        a["attempt_id"],
                        cid,
                        turn,
                        a["attempt_no"],
                        a["draft_text"],
                        a.get("model", ""),
                        json.dumps(a.get("code_checks", [])),
                        a.get("created_at", now.isoformat()),
                    ),
                )
            reply_attempts = iter(attempts)
            for v in state.get("verdicts") or []:
                rec = dict(v.get("judgement") or {})
                target = rec.get("target", "reply")
                attempt = next(reply_attempts, None) if target == "reply" else None
                jid = rec.get("judgement_id") or _new_id("J-")
                rec.setdefault("judgement_id", jid)
                rec.setdefault("decision", v.get("decision"))
                rec.setdefault("verdict", v.get("verdict"))
                self._db.execute(
                    "INSERT OR IGNORE INTO judgements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        jid,
                        cid,
                        turn,
                        attempt["attempt_id"] if attempt else None,
                        target,
                        rec.get("severity_tier") or triage.get("tier"),
                        rec.get("verdict"),
                        rec.get("decision"),
                        rec.get("decision_reason", ""),
                        json.dumps(rec),
                        rec.get("created_at", now.isoformat()),
                    ),
                )
            ticket = state.get("ticket")
            if ticket:
                self._db.execute(
                    "INSERT OR IGNORE INTO handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                    (
                        ticket["ticket_id"],
                        cid,
                        ticket["tier"],
                        ticket["urgency"],
                        ticket.get("status", "open"),
                        ticket.get("reason", ""),
                        ticket["sla_due_at"],
                        json.dumps(ticket),
                        now.isoformat(),
                    ),
                )
            new_events = []
            for seq, e in enumerate(state.get("events") or []):
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO events (conversation_id, seq, event, payload, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cid, seq, e["event"], json.dumps(e.get("payload", {})), e.get("ts")),
                )
                if cur.rowcount:
                    new_events.append(e)
        return new_events

    def set_handoff_status(
        self, ticket_id: str, status: str, now: datetime, resolution: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM handoffs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                return None
            ack = (
                now.isoformat()
                if status in ("acknowledged", "resolved") and not row["acknowledged_at"]
                else row["acknowledged_at"]
            )
            self._db.execute(
                "UPDATE handoffs SET status = ?, acknowledged_at = ?, resolution = ? WHERE ticket_id = ?",
                (
                    status,
                    ack,
                    json.dumps(resolution) if resolution else row["resolution"],
                    ticket_id,
                ),
            )
        return self.handoff(ticket_id)

    def add_correction(
        self, judgement_id: str | None, split: str, record: dict[str, Any], now: datetime
    ) -> str:
        cid = _new_id("K-")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO corrections VALUES (?, ?, ?, ?, ?)",
                (cid, judgement_id, split, json.dumps(record), now.isoformat()),
            )
        return cid

    def add_example(self, record: dict[str, Any], now: datetime) -> str:
        eid = _new_id("E-")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO examples VALUES (?, ?, ?)", (eid, json.dumps(record), now.isoformat())
            )
        return eid

    def put_proposal(self, dimension: str, change: dict[str, Any], now: datetime) -> str:
        pid = _new_id("P-")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, 'open', ?)",
                (pid, dimension, json.dumps(change), now.isoformat()),
            )
        return pid

    def set_proposal_status(self, proposal_id: str, status: str) -> bool:
        with self._lock, self._db:
            return (
                self._db.execute(
                    "UPDATE proposals SET status = ? WHERE proposal_id = ?", (status, proposal_id)
                ).rowcount
                == 1
            )

    def add_notification(
        self, kind: str, ticket_id: str | None, channel: str, text: str, now: datetime
    ) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO notifications (kind, ticket_id, channel, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, ticket_id, channel, text, now.isoformat()),
            )
            return int(cur.lastrowid or 0)

    def notifications(
        self, kind: str | None = None, ticket_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._rows("SELECT * FROM notifications ORDER BY id DESC")]
        return [
            r
            for r in rows
            if (kind is None or r["kind"] == kind)
            and (ticket_id is None or r["ticket_id"] == ticket_id)
        ]

    # ---------------------------------------------------------------- reads

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args).fetchall())

    def conversation(self, cid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM conversations WHERE id = ?", (cid,))
        if not rows:
            return None
        conv = dict(rows[0])
        conv["messages"] = [
            dict(r)
            for r in self._rows(
                "SELECT seq, role, text, at FROM messages WHERE conversation_id = ? ORDER BY seq",
                (cid,),
            )
        ]
        return conv

    def judgements(
        self,
        cid: str | None = None,
        *,
        verdict: str | None = None,
        decision: str | None = None,
        tier: str | None = None,
        dimension_failed: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM judgements WHERE 1 = 1", list[Any]()
        for col, val in (
            ("conversation_id", cid),
            ("verdict", verdict),
            ("decision", decision),
            ("tier", tier),
        ):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        if since:
            sql += " AND created_at >= ?"
            args.append(since)
        if until:
            sql += " AND created_at < ?"
            args.append(until)
        if cursor:
            sql += " AND created_at < ?"
            args.append(cursor)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit * (5 if dimension_failed else 1))
        out = [self._judgement_row(r) for r in self._rows(sql, tuple(args))]
        if dimension_failed:
            out = [
                j
                for j in out
                if any(
                    f.get("dimension") == dimension_failed for f in j["record"].get("failures", [])
                )
            ]
        return out[:limit]

    def _judgement_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["record"] = json.loads(d["record"])
        return d

    def judgement(self, jid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM judgements WHERE judgement_id = ?", (jid,))
        return self._judgement_row(rows[0]) if rows else None

    def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))
        if not rows:
            return None
        a = dict(rows[0])
        a["checks"] = json.loads(a["checks"])
        return a

    def next_attempt(self, attempt: dict[str, Any]) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT attempt_id FROM attempts WHERE conversation_id = ? AND turn = ? AND attempt_no = ?",
            (attempt["conversation_id"], attempt["turn"], attempt["attempt_no"] + 1),
        )
        return self.attempt(rows[0]["attempt_id"]) if rows else None

    def handoffs(self, status: str | None = None, tier: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM handoffs WHERE 1 = 1", list[Any]()
        if status:
            sql += " AND status = ?"
            args.append(status)
        if tier:
            sql += " AND tier = ?"
            args.append(tier)
        return [
            self._handoff_row(r) for r in self._rows(sql + " ORDER BY created_at DESC", tuple(args))
        ]

    def _handoff_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["record"] = json.loads(d["record"])
        d["resolution"] = json.loads(d["resolution"]) if d["resolution"] else None
        return d

    def handoff(self, ticket_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM handoffs WHERE ticket_id = ?", (ticket_id,))
        return self._handoff_row(rows[0]) if rows else None

    def events_after(
        self, cid: str | None, after_id: int, event: str | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE id > ?", list[Any]([after_id])
        if cid:
            sql += " AND conversation_id = ?"
            args.append(cid)
        if event:
            sql += " AND event = ?"
            args.append(event)
        return [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in self._rows(sql + " ORDER BY id", tuple(args))
        ]

    def corrections(
        self, dimension: str | None = None, split: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {**dict(r), "record": json.loads(r["record"])}
            for r in self._rows("SELECT * FROM corrections ORDER BY created_at DESC")
        ]
        if split:
            rows = [r for r in rows if r["split"] == split]
        if dimension:
            rows = [r for r in rows if dimension in r["record"].get("dimension_overrides", {})]
        return rows

    def examples(self) -> list[dict[str, Any]]:
        return [
            {**dict(r), "record": json.loads(r["record"])}
            for r in self._rows("SELECT * FROM examples ORDER BY created_at DESC")
        ]

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            {**dict(r), "change": json.loads(r["change"])}
            for r in self._rows("SELECT * FROM proposals ORDER BY created_at DESC")
        ]
        return [r for r in rows if status is None or r["status"] == status]

    def proposal(self, pid: str) -> dict[str, Any] | None:
        rows = [r for r in self.proposals() if r["proposal_id"] == pid]
        return rows[0] if rows else None
