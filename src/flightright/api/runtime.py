"""Everything the API needs, wired once: services, ops store, live event broker, n8n publisher,
Workbench exemplars and proposals, metrics and alerts."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import queue
import statistics
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from judgekit import Exemplar

from flightright.agent.graph import Deps
from flightright.agent.judging import Judge
from flightright.agent.live import load_scenarios, scenario_clock
from flightright.agent.service import ConversationService, MessageOutcome
from flightright.api.store import OpsStore
from flightright.config import ROOT, AppConfig
from flightright.llm.client import LLMClient
from flightright.models import Booking
from flightright.rules.engine import EntitlementEngine
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.triage.triage import Triage

_current_booking: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "booking", default=None
)


# ---------------------------------------------------------------- outbound events (SPEC §7.4)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@dataclass
class EventPublisher:
    """POSTs signed events to n8n; retries with backoff; never blocks a reply."""

    url: str | None
    secret: str
    retries: int = 3
    transport: httpx.BaseTransport | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    synchronous: bool = False  # tests

    def publish(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = {
            "event_id": "EV-" + uuid.uuid4().hex[:16],
            "event": event,
            "payload": payload,
            "ts": datetime.now(UTC).isoformat(),
        }
        if not self.url:
            return envelope
        if self.synchronous:
            self._send(envelope)
        else:
            threading.Thread(target=self._send, args=(envelope,), daemon=True).start()
        return envelope

    def _send(self, envelope: dict[str, Any]) -> None:
        body = json.dumps(envelope).encode()
        for attempt in range(self.retries):
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": sign(self.secret, ts, body),
                "X-Event-Id": envelope["event_id"],
            }
            try:
                with httpx.Client(timeout=10, transport=self.transport) as http:
                    r = http.post(self.url or "", content=body, headers=headers)
                if r.status_code < 500:
                    self.sent.append(envelope)
                    return
            except httpx.HTTPError:
                pass
            if not self.synchronous:
                time.sleep(2**attempt)
        self.failed.append(envelope)


def verify(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
    window_s: int = 300,
) -> bool:
    """Signature check for inbound webhooks (replay window 5 min, NFR-5)."""
    try:
        age = abs((now or time.time()) - int(timestamp))
    except ValueError:
        return False
    return age <= window_s and hmac.compare_digest(sign(secret, timestamp, body), signature)


# ---------------------------------------------------------------- live event broker (SSE)


class Broker:
    """Fan-out of pipeline events to SSE subscribers (per conversation, or all)."""

    def __init__(self) -> None:
        self._subs: dict[str | None, list[queue.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, cid: str | None) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs[cid].append(q)
        return q

    def unsubscribe(self, cid: str | None, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subs.get(cid, []):
                self._subs[cid].remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = [*self._subs.get(event.get("conversation_id"), []), *self._subs.get(None, [])]
        for q in targets:
            with contextlib.suppress(queue.Full):
                q.put_nowait(event)


# ---------------------------------------------------------------- runtime


@dataclass
class RuntimeOptions:
    demo_clock: bool = True  # run each scenario booking at its scenario's moment
    register_scenarios: bool = True  # dev-split bookings available for demos
    ops_path: Path | str = ROOT / ".cache" / "stores" / "ops.sqlite3"
    checkpoint_path: Path | str = ROOT / ".cache" / "stores" / "checkpoints.sqlite3"
    actions_path: Path | str = ROOT / ".cache" / "stores" / "actions.sqlite3"
    history_path: Path | str = ROOT / ".cache" / "stores" / "history.sqlite3"


class Runtime:
    def __init__(
        self,
        cfg: AppConfig,
        client: LLMClient | None,
        engine: EntitlementEngine,
        judge_factory: Callable[[Runtime], Judge],
        options: RuntimeOptions | None = None,
        publisher: EventPublisher | None = None,
        raw_config: dict[str, Any] | None = None,
        triage: Triage | None = None,
    ) -> None:
        self.cfg = cfg
        self.raw = raw_config or yaml.safe_load(
            (ROOT / "config" / "app_config.yaml").read_text(encoding="utf-8")
        )
        self.options = options or RuntimeOptions()
        self.client = client
        self.engine = engine
        self.store = OpsStore(self.options.ops_path)
        self.broker = Broker()
        self.publisher = publisher or EventPublisher(
            os.environ.get("N8N_EVENTS_WEBHOOK_URL") or None, os.environ.get("N8N_HMAC_SECRET", "")
        )
        self.booking_clocks: dict[str, datetime] = {}
        self.airline = MockAirline(
            cfg.authority, ActionStore(self.options.actions_path), seed=cfg.seed, clock=self.now
        )
        self.history = HistoryStore(self.options.history_path, clock=self.now)
        if (
            self.options.register_scenarios
            and (ROOT / "data" / "scenarios" / "scenarios.jsonl").is_file()
        ):
            for s in load_scenarios("dev"):
                self.register_booking(
                    Booking(
                        booking_ref=s.booking_ref,
                        passenger_ref=s.passenger_ref,
                        passenger_name=s.persona.name,
                        loyalty_tier=s.persona.loyalty_tier,
                        case=s.case,
                    ),
                    clock=scenario_clock(s),
                )
        tools = AgentTools(self.airline, self.history, Playbooks(), clock=self.now)
        self.judge = judge_factory(self)
        deps = Deps(
            config=cfg,
            engine=engine,
            triage=triage or Triage(client),
            client=client,
            tools=tools,
            judge=self.judge,
            clock=self.now,
        )
        self.service = ConversationService(deps, self.options.checkpoint_path)
        self._alerted: dict[str, datetime] = {}

    # ---------------------------------------------------------------- clock and bookings

    def now(self) -> datetime:
        ref = _current_booking.get()
        if self.options.demo_clock and ref and ref in self.booking_clocks:
            return self.booking_clocks[ref]
        return datetime.now(UTC)

    def register_booking(self, booking: Booking, clock: datetime | None = None) -> None:
        self.airline.register_booking(booking)
        if clock is not None:
            self.booking_clocks[booking.booking_ref.upper()] = clock

    # ---------------------------------------------------------------- conversations

    def create_conversation(
        self, channel: str, passenger_ref: str | None, booking_ref: str | None
    ) -> str:
        cid = self.service.create_conversation(
            channel, passenger_ref=passenger_ref, booking_ref=booking_ref
        )
        self.store.upsert_conversation(
            cid,
            channel=channel,
            booking_ref=booking_ref,
            passenger_ref=passenger_ref,
            status="active",
            tier=None,
            turns=0,
            now=datetime.now(UTC),
        )
        return cid

    def handle_message(self, cid: str, text: str, idempotency_key: str) -> MessageOutcome:
        state = self.service.state(cid)
        if not state:
            raise KeyError(cid)
        token = _current_booking.set((state.get("booking_ref") or "").upper() or None)
        try:
            out = self.service.handle_message(
                cid, text, idempotency_key, on_event=self._on_event(cid)
            )
            if out.duplicate:
                return out
            final = self.service.state(cid)
            if final.get("booking_ref") and final["booking_ref"].upper() != (
                _current_booking.get() or ""
            ):
                _current_booking.set(
                    final["booking_ref"].upper()
                )  # booking found in the message text
            self.store.record_turn(final, datetime.now(UTC))
        finally:
            _current_booking.reset(token)
        self.check_alerts()
        return out

    def _on_event(self, cid: str) -> Callable[[dict[str, Any]], None]:
        def emit(event: dict[str, Any]) -> None:
            self.broker.publish(event)
            if event["event"] in ("handoff.created", "guardrail.injection_detected"):
                payload = {**event.get("payload", {}), "conversation_id": cid}
                self.publisher.publish(event["event"], payload)

        return emit

    # ---------------------------------------------------------------- Workbench (FR-16)

    def exemplars(self, tier: str, target: str) -> Sequence[Exemplar]:
        judge_cfg = self.raw.get("judge", {})
        if not judge_cfg.get("exemplars_enabled", True):
            return []
        out: list[Exemplar] = []
        for c in self.store.corrections():
            if c["split"] not in ("dev", "live"):
                continue  # FR-16.5: never from the held-out test split
            rec = c["record"]
            if rec.get("tier") not in (None, tier):
                continue
            for dim, value in rec.get("dimension_overrides", {}).items():
                out.append(
                    Exemplar(
                        dimension=dim,
                        excerpt=(rec.get("excerpt") or "")[:240],
                        score=value
                        if isinstance(value, int) and not isinstance(value, bool)
                        else None,
                        passed=value if isinstance(value, bool) else None,
                        reason=rec.get("reason") or "",
                    )
                )
        return out[: int(judge_cfg.get("exemplars_k", 3))]

    def propose(self, now: datetime) -> list[str]:
        """FR-16.4: when reviewers consistently disagree with the judge on a dimension in one
        direction,
        propose moving that dimension's bar for the tier. Applied only on explicit approval."""
        need = int(self.raw.get("judge", {}).get("proposal_min_corrections", 3))
        votes: dict[tuple[str, str], list[int]] = defaultdict(list)
        for c in self.store.corrections():
            rec = c["record"]
            for dim, value in rec.get("dimension_overrides", {}).items():
                orig = (rec.get("original_scores") or {}).get(dim)
                if isinstance(value, int) and not isinstance(value, bool) and isinstance(orig, int):
                    votes[(rec.get("tier") or "S2", dim)].append(value - orig)
        existing = {(p["change"]["tier"], p["dimension"]) for p in self.store.proposals("open")}
        made = []
        for (tier, dim), deltas in votes.items():
            if (
                len(deltas) >= need
                and (tier, dim) not in existing
                and (all(d < 0 for d in deltas) or all(d > 0 for d in deltas))
            ):
                direction = "raise" if deltas[0] < 0 else "lower"
                made.append(
                    self.store.put_proposal(
                        dim,
                        {
                            "tier": tier,
                            "action": f"{direction}_min_score",
                            "evidence": f"{len(deltas)} corrections, mean delta "
                            f"{statistics.mean(deltas):+.1f}",
                        },
                        now,
                    )
                )
        return made

    # ---------------------------------------------------------------- metrics (FR-17.2)

    def judge_metrics(self, window: timedelta | None) -> dict[str, Any]:
        since = (datetime.now(UTC) - window).isoformat() if window else None
        rows = [
            j for j in self.store.judgements(since=since, limit=100_000) if j["target"] == "reply"
        ]
        by_tier: dict[str, Counter[str]] = defaultdict(Counter)
        dim_fail: Counter[str] = Counter()
        dim_seen: Counter[str] = Counter()
        latencies, disagree, panels, unavailable = [], 0, 0, 0
        models: Counter[str] = Counter()
        for j in rows:
            rec = j["record"]
            by_tier[j["tier"] or "?"][j["decision"] or "?"] += 1
            for s in rec.get("scores", []):
                dim_seen[s["id"]] += 1
            for f in rec.get("failures", []):
                dim_fail[f["dimension"]] += 1
            if rec.get("latency_ms") is not None:
                latencies.append(rec["latency_ms"])
            if rec.get("panel"):
                panels += 1
                disagree += bool(rec["panel"].get("disagreement"))
            unavailable += bool(rec.get("error"))
            models.update((rec.get("models") or {}).keys())
        lat = sorted(latencies)
        quota = {}
        if self.client is not None:
            for key, lim in self.client.cfg.catalog.items():
                req, tok = self.client.ledger.today(key)
                if req:
                    quota[key] = {"requests": req, "tokens": tok, "rpd_limit": lim.rpd}
        return {
            "window": str(window) if window else "all",
            "judgements": len(rows),
            "decisions_by_tier": {t: dict(c) for t, c in sorted(by_tier.items())},
            "dimension_pass_rate": {
                d: 1 - dim_fail[d] / n for d, n in sorted(dim_seen.items()) if n
            },
            "panel_disagreement_rate": disagree / panels if panels else None,
            "judge_unavailable_rate": unavailable / len(rows) if rows else None,
            "latency_ms_p50": lat[len(lat) // 2] if lat else None,
            "latency_ms_p95": lat[max(0, int(len(lat) * 0.95) - 1)] if lat else None,
            "models": dict(models),
            "quota_today": quota,
            "quality_signals": self.quality_signals(),
        }

    def quality_signals(self) -> dict[str, Any]:
        """FR-17.3: latest offline quality numbers (eval / canary runs) plus reviewer agreement."""
        out: dict[str, Any] = {}
        for name in ("planted_errors", "judge_consistency", "fairness"):
            path = ROOT / "data" / "eval" / f"{name}.json"
            if path.is_file():
                out[name] = json.loads(path.read_text(encoding="utf-8"))
        corrections = self.store.corrections()
        if corrections:
            agree = sum(1 for c in corrections if c["record"].get("agrees_with_decision"))
            out["reviewer_agreement"] = agree / len(corrections)
            out["corrections"] = len(corrections)
        return out

    def check_alerts(self) -> list[str]:
        """FR-17.5: emit metrics.threshold_breached (deduplicated) when a rolling rate crosses
        its bar."""
        a = self.raw.get("alerts", {})
        m = self.judge_metrics(timedelta(hours=1))
        if m["judgements"] < int(a.get("min_judgements", 10)):
            return []
        breached = []
        for key in ("panel_disagreement_rate", "judge_unavailable_rate"):
            value, limit = m.get(key), a.get(key)
            if value is not None and limit is not None and value > float(limit):
                breached.append((key, value, limit))
        handoffs = sum(c.get("handoff", 0) for c in m["decisions_by_tier"].values())
        rate = handoffs / m["judgements"]
        if a.get("handoff_rate") is not None and rate > float(a["handoff_rate"]):
            breached.append(("handoff_rate", rate, a["handoff_rate"]))
        now = datetime.now(UTC)
        dedupe = timedelta(minutes=int(a.get("dedupe_minutes", 60)))
        sent = []
        for key, value, limit in breached:
            if key in self._alerted and now - self._alerted[key] < dedupe:
                continue
            self._alerted[key] = now
            self.publisher.publish(
                "metrics.threshold_breached", {"metric": key, "value": value, "limit": limit}
            )
            sent.append(key)
        return sent
