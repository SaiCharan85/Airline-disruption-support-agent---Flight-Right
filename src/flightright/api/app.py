"""FastAPI service (SPEC §7): customer API, ops API, SSE streams, Judge Workbench and Monitor.

Customer endpoints return the final reply only. Ops endpoints need `X-API-Key`; personal data is
masked unless `?unmask=true` with `X-Admin-Key` (NFR-6). Errors are RFC 7807 problem+json.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from flightright.agent.guardrails import mask_for_logs
from flightright.api.runtime import Runtime
from flightright.config import ROOT
from flightright.models import Booking
from flightright.services.history import PriorCase
from flightright.services.history import Resolution as HistoryResolution

WINDOWS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "all": None,
}


# ---------------------------------------------------------------- request / response models


class ConversationIn(BaseModel):
    channel: str = "webhook"
    passenger_ref: str | None = None
    booking_ref: str | None = None


class ConversationOut(BaseModel):
    conversation_id: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class MessageOut(BaseModel):
    reply_text: str
    status: Literal["answered", "handed_off"]
    ticket_id: str | None = None
    duplicate: bool = False


class ResolveIn(BaseModel):
    decision: str = Field(min_length=1)
    notes: str = ""
    compensation_paid: int | None = Field(default=None, ge=0)


class CorrectionIn(BaseModel):
    split: Literal["dev", "live", "test"] = "live"
    dimension_overrides: dict[str, int | bool] = Field(default_factory=dict)
    verdict_override: Literal["good", "acceptable", "bad"] | None = None
    decision_override: Literal["approve", "revise", "handoff"] | None = None
    reason: str = ""


class ExampleIn(BaseModel):
    prompt_text: str = Field(min_length=1)
    expected_reply: str = Field(min_length=1)
    notes: str = ""
    split: Literal["dev", "live", "test"] = "live"


class NotificationIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)  # handoff.created, sla.escalated, alert, digest
    ticket_id: str | None = None
    channel: str = "log"
    text: str = Field(min_length=1, max_length=10_000)


class BookingIn(BaseModel):
    booking: Booking
    demo_clock: datetime | None = None


def problem(status: int, title: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
    )


def _git_commit() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            return (ROOT / ".git" / ref[5:]).read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


# ---------------------------------------------------------------- app


def create_app(runtime: Runtime, api_key: str, admin_key: str) -> FastAPI:
    app = FastAPI(
        title="flightright-agent",
        version="0.1.0",
        description="Disruption support with a severity-aware LLM judge gating every reply.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "invalid request", json.loads(json.dumps(exc.errors(), default=str)))

    def ops(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key != api_key:
            raise HTTPException(401, "missing or invalid X-API-Key")

    def unmask_allowed(
        unmask: bool = Query(default=False), x_admin_key: str | None = Header(default=None)
    ) -> bool:
        if unmask and x_admin_key != admin_key:
            raise HTTPException(403, "unmasking requires X-Admin-Key")
        return unmask

    def mask(obj: Any, cid: str | None, unmask: bool) -> Any:
        if unmask or not cid:
            return obj
        conv = runtime.store.conversation(cid) or {}
        ref = conv.get("booking_ref")
        booking = runtime.airline.get_booking(ref) if ref else None
        name = booking.passenger_name if booking else None

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                return mask_for_logs(x, ref, name)
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            if isinstance(x, list):
                return [walk(v) for v in x]
            return x

        return walk(obj)

    # ---------------------------------------------------------------- customer API (§7.1)

    @app.post("/conversations", response_model=ConversationOut, status_code=201)
    def create_conversation(body: ConversationIn) -> ConversationOut:
        return ConversationOut(
            conversation_id=runtime.create_conversation(
                body.channel, body.passenger_ref, body.booking_ref
            )
        )

    @app.post("/conversations/{cid}/messages", response_model=MessageOut)
    def post_message(cid: str, body: MessageIn) -> MessageOut:
        try:
            out = runtime.handle_message(cid, body.text, body.idempotency_key)
        except KeyError:
            raise HTTPException(404, f"unknown conversation {cid}") from None
        return MessageOut(**out.model_dump())

    # ------------------------------------------------ ops: conversations and judgements (§7.2)

    @app.get("/ops/conversations/{cid}", dependencies=[Depends(ops)])
    def get_conversation(cid: str, unmask: bool = Depends(unmask_allowed)) -> dict[str, Any]:
        conv = runtime.store.conversation(cid)
        if conv is None:
            raise HTTPException(404, f"unknown conversation {cid}")
        state = runtime.service.state(cid)
        conv["triage"] = state.get("triage")
        conv["entitlement"] = state.get("entitlement")
        conv["ticket"] = state.get("ticket")
        return mask(conv, cid, unmask)  # type: ignore[no-any-return]

    @app.get("/ops/conversations/{cid}/judgements", dependencies=[Depends(ops)])
    def conversation_judgements(
        cid: str, unmask: bool = Depends(unmask_allowed)
    ) -> list[dict[str, Any]]:
        rows = sorted(runtime.store.judgements(cid, limit=1000), key=lambda j: j["created_at"])
        return mask(rows, cid, unmask)  # type: ignore[no-any-return]

    @app.get("/ops/judgements", dependencies=[Depends(ops)])
    def list_judgements(
        verdict: str | None = None,
        decision: str | None = None,
        tier: str | None = None,
        dimension_failed: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = runtime.store.judgements(
            verdict=verdict,
            decision=decision,
            tier=tier,
            dimension_failed=dimension_failed,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
        )
        items = [mask(r, r["conversation_id"], False) for r in rows]
        return {
            "items": items,
            "next_cursor": rows[-1]["created_at"] if len(rows) == limit else None,
        }

    @app.get("/ops/judgements/stream", dependencies=[Depends(ops)])
    async def judgements_stream(request: Request, once: bool = False) -> StreamingResponse:
        return StreamingResponse(
            _sse(request, None, {"judgement.completed"}, once), media_type="text/event-stream"
        )

    @app.get("/ops/judgements/{jid}", dependencies=[Depends(ops)])
    def get_judgement(jid: str, unmask: bool = Depends(unmask_allowed)) -> dict[str, Any]:
        j = runtime.store.judgement(jid)
        if j is None:
            raise HTTPException(404, f"unknown judgement {jid}")
        j["attempt"] = runtime.store.attempt(j["attempt_id"]) if j["attempt_id"] else None
        return mask(j, j["conversation_id"], unmask)  # type: ignore[no-any-return]

    @app.get("/ops/judgements/{jid}/diff", dependencies=[Depends(ops)])
    def judgement_diff(jid: str, unmask: bool = Depends(unmask_allowed)) -> dict[str, Any]:
        j = runtime.store.judgement(jid)
        if j is None or not j["attempt_id"]:
            raise HTTPException(404, f"no draft attempt behind judgement {jid}")
        failed = runtime.store.attempt(j["attempt_id"])
        assert failed is not None
        revision = runtime.store.next_attempt(failed)
        rec = j["record"]
        guidance = [f"{f['dimension']}: {f['reason']}" for f in rec.get("failures", [])]
        if rec.get("revision_guidance"):
            guidance.append(rec["revision_guidance"])
        out = {
            "judgement_id": jid,
            "decision": j["decision"],
            "failed_draft": failed["draft"],
            "revision": revision["draft"] if revision else None,
            "guidance": guidance,
            "failed_checks": [c for c in failed["checks"] if not c["passed"]],
        }
        return mask(out, j["conversation_id"], unmask)  # type: ignore[no-any-return]

    @app.get("/ops/conversations/{cid}/stream", dependencies=[Depends(ops)])
    async def conversation_stream(
        cid: str, request: Request, once: bool = False
    ) -> StreamingResponse:
        return StreamingResponse(_sse(request, cid, None, once), media_type="text/event-stream")

    async def _sse(
        request: Request, cid: str | None, only: set[str] | None, once: bool
    ) -> AsyncIterator[str]:
        """Replay stored events, then stream live ones (FR-17.1); `once` stops after replay."""
        sub = runtime.broker.subscribe(cid)
        try:
            for e in runtime.store.events_after(cid, 0):
                if only is None or e["event"] in only:
                    yield _sse_line(
                        e["event"],
                        {
                            "event": e["event"],
                            "conversation_id": e["conversation_id"],
                            "payload": e["payload"],
                            "ts": e["ts"],
                        },
                    )
            if once:
                return
            loop = asyncio.get_running_loop()
            while not await request.is_disconnected():
                try:
                    e = await loop.run_in_executor(None, sub.get, True, 15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if only is None or e["event"] in only:
                    yield _sse_line(e["event"], e)
        finally:
            runtime.broker.unsubscribe(cid, sub)

    # ---------------------------------------------------------------- ops: handoffs

    @app.get("/ops/handoffs", dependencies=[Depends(ops)])
    def list_handoffs(
        status: str | None = None, tier: str | None = None, sla_breached: bool | None = None
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        rows = runtime.store.handoffs(status, tier)
        for r in rows:
            r["sla_breached"] = r["status"] in ("open", "escalated") and r["sla_due_at"] < now
        if sla_breached is not None:
            rows = [r for r in rows if r["sla_breached"] == sla_breached]
        return [mask(r, r["conversation_id"], False) for r in rows]

    @app.get("/ops/handoffs/{ticket_id}", dependencies=[Depends(ops)])
    def get_handoff(ticket_id: str) -> dict[str, Any]:
        h = runtime.store.handoff(ticket_id)
        if h is None:
            raise HTTPException(404, f"unknown ticket {ticket_id}")
        return mask(h, h["conversation_id"], False)  # type: ignore[no-any-return]

    @app.post("/ops/handoffs/{ticket_id}/acknowledge", dependencies=[Depends(ops)])
    def acknowledge(ticket_id: str) -> dict[str, Any]:
        h = runtime.store.set_handoff_status(ticket_id, "acknowledged", datetime.now(UTC))
        if h is None:
            raise HTTPException(404, f"unknown ticket {ticket_id}")
        return h

    @app.post("/ops/handoffs/{ticket_id}/escalate", dependencies=[Depends(ops)])
    def escalate(ticket_id: str) -> dict[str, Any]:
        current = runtime.store.handoff(ticket_id)
        if current is None:
            raise HTTPException(404, f"unknown ticket {ticket_id}")
        if current["status"] != "open":
            raise HTTPException(409, f"ticket is {current['status']}, not open")
        h = runtime.store.set_handoff_status(ticket_id, "escalated", datetime.now(UTC))
        assert h is not None
        return h

    @app.post("/ops/handoffs/{ticket_id}/resolve", dependencies=[Depends(ops)])
    def resolve(ticket_id: str, body: ResolveIn) -> dict[str, Any]:
        h = runtime.store.handoff(ticket_id)
        if h is None:
            raise HTTPException(404, f"unknown ticket {ticket_id}")
        now = datetime.now(UTC)
        resolution = {
            "decision": body.decision,
            "notes": body.notes,
            "compensation_paid_eur": body.compensation_paid,
            "resolved_at": now.isoformat(),
        }
        conv = runtime.store.conversation(h["conversation_id"]) or {}
        passenger_ref = conv.get("passenger_ref")
        if not passenger_ref and conv.get(
            "booking_ref"
        ):  # handed off before the booking was loaded
            booking = runtime.airline.get_booking(conv["booking_ref"])
            passenger_ref = booking.passenger_ref if booking else None
        if passenger_ref:  # FR-9.5: stored as episodic memory
            runtime.history.add_case(
                passenger_ref,
                PriorCase(
                    case_id=ticket_id,
                    booking_ref=conv.get("booking_ref") or "",
                    disruption_type="handoff",
                    summary=h["record"].get("summary", ""),
                    status="open",
                    opened_at=now,
                ),
            )
            runtime.history.add_resolution(
                HistoryResolution(
                    case_id=ticket_id,
                    decision=body.decision,
                    notes=body.notes,
                    compensation_paid_eur=body.compensation_paid,
                    resolved_at=now,
                )
            )
        out = runtime.store.set_handoff_status(ticket_id, "resolved", now, resolution)
        assert out is not None
        return out

    # ---------------------------------------------------------------- ops: Judge Workbench (FR-16)

    @app.post("/ops/judgements/{jid}/corrections", status_code=201, dependencies=[Depends(ops)])
    def add_correction(jid: str, body: CorrectionIn) -> dict[str, Any]:
        if body.split == "test":
            raise HTTPException(
                400, "corrections on the held-out test split are not accepted (FR-16.5)"
            )
        j = runtime.store.judgement(jid)
        if j is None:
            raise HTTPException(404, f"unknown judgement {jid}")
        rec = j["record"]
        original = {
            s["id"]: s["score"] if s["kind"] == "scale" else s["passed"]
            for s in rec.get("scores", [])
        }
        attempt = runtime.store.attempt(j["attempt_id"]) if j["attempt_id"] else None
        unknown = set(body.dimension_overrides) - set(original) if original else set()
        if unknown:
            raise HTTPException(422, f"unknown dimensions: {sorted(unknown)}")
        record = {
            **body.model_dump(),
            "tier": j["tier"],
            "original_scores": original,
            "original_decision": j["decision"],
            "excerpt": (attempt or {}).get("draft", "")[:400],
            "agrees_with_decision": body.decision_override in (None, j["decision"])
            and not body.dimension_overrides,
        }
        cid = runtime.store.add_correction(jid, body.split, record, datetime.now(UTC))
        proposals = runtime.propose(datetime.now(UTC))
        return {"correction_id": cid, "new_proposals": proposals}

    @app.post("/ops/judge/examples", status_code=201, dependencies=[Depends(ops)])
    def add_example(body: ExampleIn) -> dict[str, str]:
        if body.split == "test":
            raise HTTPException(
                400, "examples from the held-out test split are not accepted (FR-16.5)"
            )
        return {"example_id": runtime.store.add_example(body.model_dump(), datetime.now(UTC))}

    @app.get("/ops/judge/corrections", dependencies=[Depends(ops)])
    def list_corrections(
        dimension: str | None = None, split: str | None = None
    ) -> list[dict[str, Any]]:
        return runtime.store.corrections(dimension, split)

    @app.get("/ops/judge/examples", dependencies=[Depends(ops)])
    def list_examples() -> list[dict[str, Any]]:
        return runtime.store.examples()

    @app.get("/ops/judge/proposals", dependencies=[Depends(ops)])
    def list_proposals(status: str | None = None) -> list[dict[str, Any]]:
        return runtime.store.proposals(status)

    @app.post("/ops/judge/proposals/{pid}/apply", dependencies=[Depends(ops)])
    def apply_proposal(pid: str) -> dict[str, Any]:
        p = runtime.store.proposal(pid)
        if p is None:
            raise HTTPException(404, f"unknown proposal {pid}")
        if p["status"] != "open":
            raise HTTPException(409, f"proposal is {p['status']}")
        rubric_version = runtime_apply(p)
        runtime.store.set_proposal_status(pid, "applied")
        return {"proposal_id": pid, "status": "applied", "rubric_version": rubric_version}

    def runtime_apply(p: dict[str, Any]) -> str:
        judge = runtime.judge
        rubric = getattr(judge, "rubric", None)
        if rubric is None:
            raise HTTPException(409, "the active judge has no rubric to change")
        tier, dim, action = p["change"]["tier"], p["dimension"], p["change"]["action"]
        profile = rubric.profile(tier)
        current = profile.min_scores.get(dim, 1)
        new = min(5, current + 1) if action.startswith("raise") else max(1, current - 1)
        new_profile = profile.model_copy(update={"min_scores": {**profile.min_scores, dim: new}})
        major, _, minor = rubric.version.partition("+p")
        version = f"{major}+p{int(minor or 0) + 1}"
        new_rubric = rubric.model_copy(
            update={"profiles": {**rubric.profiles, tier: new_profile}, "version": version}
        )
        judge.rubric = new_rubric  # type: ignore[attr-defined]
        for member in (
            getattr(judge, "judge_a", None),
            getattr(judge, "judge_b", None),
            getattr(judge, "panel", None),
        ):
            if member is not None:
                member.rubric = new_rubric
        return version

    # ------------------------------------------------ ops: metrics (FR-17) and health

    @app.get("/ops/metrics/judge", dependencies=[Depends(ops)])
    def judge_metrics(
        window: str = Query(default="24h", pattern="^(15m|1h|24h|7d|all)$"),
    ) -> dict[str, Any]:
        return runtime.judge_metrics(WINDOWS[window])

    @app.get("/ops/metrics/fairness", dependencies=[Depends(ops)])
    def fairness() -> dict[str, Any]:
        path = ROOT / "data" / "eval" / "fairness.json"
        if not path.is_file():
            return {
                "available": False,
                "detail": "run the evaluation to compute counterfactual gaps",
            }
        return {"available": True, **json.loads(path.read_text(encoding="utf-8"))}

    @app.post("/ops/alerts/check", dependencies=[Depends(ops)])
    def alerts_check() -> dict[str, list[str]]:
        """W4's 15-minute poll: re-evaluate the rolling thresholds (alerts go out as events)."""
        return {"breached": runtime.check_alerts()}

    @app.post("/ops/notifications", status_code=201, dependencies=[Depends(ops)])
    def add_notification(body: NotificationIn) -> dict[str, int]:
        """Where n8n logs handoff notices, SLA escalations, alerts and digests."""
        nid = runtime.store.add_notification(
            body.kind, body.ticket_id, body.channel, body.text, datetime.now(UTC)
        )
        return {"id": nid}

    @app.get("/ops/notifications", dependencies=[Depends(ops)])
    def list_notifications(
        kind: str | None = None, ticket_id: str | None = None
    ) -> list[dict[str, Any]]:
        return runtime.store.notifications(kind, ticket_id)

    @app.post("/ops/bookings", status_code=201, dependencies=[Depends(ops)])
    def register_booking(body: BookingIn) -> dict[str, str]:
        """Register a booking for demos and the n8n acceptance test (mock airline)."""
        runtime.register_booking(body.booking, body.demo_clock)
        return {"booking_ref": body.booking.booking_ref}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        rulesets = {name: e["version"] for name, e in runtime.engine.rulesets.items()}
        rubric = getattr(runtime.judge, "rubric", None)
        return {
            "status": "ok",
            "git_commit": _git_commit(),
            "rulesets": rulesets,
            "rubric_id": f"{rubric.name}@{rubric.version}" if rubric else "code_checks_only",
            "time": datetime.now(UTC).isoformat(),
        }

    return app


def _sse_line(event: str, data: dict[str, Any]) -> str:
    safe = re.sub(r"[\r\n]", " ", event)
    return f"event: {safe}\ndata: {json.dumps(data, default=str)}\n\n"
