"""The conversation graph (FR-1, FR-2, FR-6, FR-7, FR-9) on LangGraph.

One thread per conversation (thread_id = conversation_id), checkpointed in SQLite. State holds only
JSON-able values so any checkpoint can be inspected and resumed. Code decides every route; the LLM
only writes text (the reply draft and the handoff summary).

    intake -> guardrails -> context -> triage -> entitlement -> care_actions -> draft -> checks
           -> judge -> (approve: finish | revise: draft | handoff: handoff -> finish)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from flightright.agent import checks as code_checks
from flightright.agent import drafting, guardrails, handoff
from flightright.agent.judging import Judge
from flightright.config import AppConfig
from flightright.llm.client import LLMClient
from flightright.llm.types import LLMUnavailableError
from flightright.models import (
    Booking,
    CheckResult,
    DraftAttempt,
    Entitlement,
    HandoffTicket,
    TriageResult,
)
from flightright.rules.engine import EntitlementEngine
from flightright.services.tools import AgentTools
from flightright.services.types import CareVoucherType, ServiceError
from flightright.triage.rules import TriageContext
from flightright.triage.triage import Triage

REFUND_REQUEST = re.compile(
    r"\b(refund|money back|reimburse\w*|cancel (my|the) (trip|booking))\b", re.I
)
REROUTING_TYPES = ("cancellation", "denied_boarding", "missed_connection")
BOOKED_FLAGS = ("reduced_mobility", "infant", "unaccompanied_minor")
PER_TURN_KEYS = (
    "guard",
    "triage",
    "booking",
    "entitlement",
    "context",
    "actions",
    "voucher_amounts",
    "refund_amount",
    "attempts",
    "verdicts",
    "guidance",
    "handoff_reason",
    "reply",
    "outcome",
)


class State(TypedDict, total=False):
    # conversation (persists across turns)
    conversation_id: str
    channel: str
    passenger_ref: str | None
    booking_ref: str | None
    transcript: list[dict[str, str]]
    processed: dict[str, dict[str, Any]]
    abuse_count: int
    turn: int
    status: str
    ticket: dict[str, Any] | None
    events: list[dict[str, Any]]
    # one turn
    incoming: dict[str, str] | None
    guard: dict[str, Any]
    triage: dict[str, Any]
    booking: dict[str, Any] | None
    entitlement: dict[str, Any] | None
    context: dict[str, Any]
    actions: list[str]
    voucher_amounts: list[int]
    refund_amount: int | None
    attempts: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    guidance: list[str]
    handoff_reason: str | None
    reply: str | None
    outcome: dict[str, Any] | None
    route: str


@dataclass
class Deps:
    config: AppConfig
    engine: EntitlementEngine
    triage: Triage
    client: LLMClient | None
    tools: AgentTools
    judge: Judge
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def _event(state: State, deps: Deps, name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *state.get("events", []),
        {
            "event": name,
            "conversation_id": state.get("conversation_id"),
            "attempt_no": len(state.get("attempts", [])),
            "payload": payload,
            "ts": deps.clock().isoformat(),
        },
    ]


def build_graph(
    deps: Deps, checkpointer: Checkpointer = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    cfg = deps.config

    # ---------------------------------------------------------------- intake and guardrails

    def intake(state: State) -> dict[str, Any]:
        incoming = state.get("incoming")
        if not incoming:
            return {
                "route": "end",
                "status": state.get("status", "active"),
                "processed": state.get("processed", {}),
                "transcript": state.get("transcript", []),
                "events": state.get("events", []),
            }
        done = state.get("processed", {}).get(incoming["idempotency_key"])
        if (
            done is not None
        ):  # FR-1.4: a duplicate delivery returns the original outcome, nothing re-runs
            return {"route": "end", "outcome": {**done, "duplicate": True}}
        deps.tools.calls.clear()  # tool calls are recorded per turn
        reset: dict[str, Any] = {k: None for k in PER_TURN_KEYS}
        reset |= {
            "actions": [],
            "voucher_amounts": [],
            "attempts": [],
            "verdicts": [],
            "guidance": [],
            "turn": state.get("turn", 0) + 1,
            "route": "guardrails",
        }
        return reset

    def guard(state: State) -> dict[str, Any]:
        incoming = state["incoming"] or {}
        g = guardrails.check(incoming["text"])
        transcript = [
            *state.get("transcript", []),
            {"role": "passenger", "text": g.clean_text, "at": deps.clock().isoformat()},
        ]
        abuse = state.get("abuse_count", 0) + int(g.abusive)
        out: dict[str, Any] = {
            "guard": g.model_dump(),
            "transcript": transcript,
            "abuse_count": abuse,
        }
        events = state.get("events", [])
        if g.injection:
            events = _event(
                {**state, "events": events},
                deps,
                "guardrail.injection_detected",
                {"turn": state.get("turn")},
            )
        out["events"] = events
        if g.booking_ref_in_text and not state.get("booking_ref"):
            out["booking_ref"] = g.booking_ref_in_text
        if g.human_requested or state.get("status") == "handed_off":
            out |= {
                "route": "handoff",
                "handoff_reason": "human_requested" if g.human_requested else "already_handed_off",
            }
        elif abuse >= cfg.agent.abuse_handoff_threshold:
            out |= {"route": "handoff", "handoff_reason": "abuse_threshold"}
        elif g.off_topic:
            out |= {"route": "redirect"}
        else:
            out["route"] = "context"
        return out

    def redirect(state: State) -> dict[str, Any]:
        return {
            "reply": "I can help with flight delays, cancellations, missed connections and denied "
            "boarding. Could you tell me what's happening with your flight, and share your "
            "booking reference if you have it?",
            "route": "finish",
        }

    # ---------------------------------------------------------------- context, triage, entitlement

    def context(state: State) -> dict[str, Any]:
        ref = state.get("booking_ref")
        booking: Booking | None = deps.tools.airline.get_booking(ref) if ref else None
        if booking is None:
            return {
                "route": "handoff" if state.get("turn", 1) > 2 else "ask_booking",
                "handoff_reason": "no_booking",
            }
        case = booking.case
        now = deps.clock()
        days = (
            case.scheduled_departure.date() - now.astimezone(case.scheduled_departure.tzinfo).date()
        ).days
        history = deps.tools.get_passenger_history(booking.passenger_ref)
        earlier = [
            c
            for c in history.contacts
            if c.booking_ref == booking.booking_ref
            and c.conversation_id != state["conversation_id"]
        ]
        playbook = deps.tools.get_playbook(case.disruption_type)
        ctx = {
            "days_until_departure": days,
            "repeat_contacts": len(earlier),
            "prior_cases": [c.summary for c in history.cases][-3:],
            "playbook": playbook.text,
            "playbook_version": playbook.version,
            "flight": {
                "flight": f"{case.flight_no} {case.origin_iata}->{case.final_destination_iata}"
                + (f" via {', '.join(case.connection_iatas)}" if case.connection_iatas else ""),
                "scheduled departure": case.scheduled_departure.isoformat(),
                "what happened": case.disruption_type.replace("_", " "),
                "departure delay (min)": case.departure_delay_minutes,
                "arrival delay (min)": case.arrival_delay_minutes,
                "notice given (days)": case.notice_given_days,
            },
            "rebooking": [],
        }
        return {
            "booking": booking.model_dump(mode="json"),
            "passenger_ref": booking.passenger_ref,
            "context": ctx,
            "route": "triage",
        }

    def ask_booking(state: State) -> dict[str, Any]:
        return {"reply": handoff.holding_message("no_booking", "S2", 0), "route": "finish"}

    def triage_node(state: State) -> dict[str, Any]:
        booking = Booking.model_validate(state["booking"])
        ctx = state["context"]
        tctx = TriageContext(
            disruption_type=booking.case.disruption_type,
            days_until_departure=ctx["days_until_departure"],
            departure_delay_minutes=booking.case.departure_delay_minutes
            if ctx["days_until_departure"] == 0
            else 0,
            booking_flags=[f for f in booking.case.passenger_flags if f in BOOKED_FLAGS],
            repeat_contact_count=ctx["repeat_contacts"],
        )
        result = deps.triage.classify(state["guard"]["clean_text"], tctx)
        return {
            "triage": result.model_dump(mode="json"),
            "events": _event(
                state, deps, "triage.completed", {"tier": result.tier, "urgency": result.urgency}
            ),
            "route": "entitlement",
        }

    def entitlement_node(state: State) -> dict[str, Any]:
        booking = Booking.model_validate(state["booking"])
        ent = deps.engine.compute_entitlement(booking.case)
        triage = TriageResult.model_validate(state["triage"])
        route = "care_actions"
        reason = None
        if triage.tier == "S4":
            route, reason = "handoff", "severity_s4"
        elif ent.confidence == "unverified":
            route, reason = "handoff", "needs_review"
        ctx = dict(state["context"])
        if ent.in_scope and (
            booking.case.disruption_type in REROUTING_TYPES
            or booking.case.departure_delay_minutes >= 300
        ):
            origin = deps.engine.airports.get(booking.case.origin_iata)
            dest = deps.engine.airports.get(booking.case.final_destination_iata)
            intra = all(
                deps.engine.territories.classify(a.country) != "other" for a in (origin, dest)
            )
            options = deps.tools.get_rebooking_options(
                booking.case.flight_no,
                booking.case.scheduled_departure.date().isoformat(),
                booking.case.carrier_iata,
                booking.case.origin_iata,
                booking.case.final_destination_iata,
                ent.distance_km or 0,
                intra,
            )
            ctx["rebooking"] = [o.model_dump(mode="json") for o in options]
        return {
            "entitlement": ent.model_dump(mode="json"),
            "context": ctx,
            "route": route,
            "handoff_reason": reason,
            "events": _event(
                state,
                deps,
                "entitlement.computed",
                {
                    "regime": ent.regime,
                    "compensation_eur": ent.compensation_eur,
                    "confidence": ent.confidence,
                },
            ),
        }

    def care_actions(state: State) -> dict[str, Any]:
        """Code decides side effects; each is idempotent per conversation (NFR-4)."""
        ent = Entitlement.model_validate(state["entitlement"])
        booking = Booking.model_validate(state["booking"])
        cid = state["conversation_id"]
        actions: list[str] = []
        amounts: list[int] = []
        refund_amount = None
        std = cfg.authority.care_voucher_standard_eur
        waiting_now = state["context"]["days_until_departure"] == 0
        try:
            if waiting_now:
                items = ent.care_items
                plan: list[tuple[CareVoucherType, int]] = []
                if items & {"meals_refreshments", "meals"}:
                    plan.append(("meal", 1))
                if items & {"refreshments", "refreshments_immediate", "meals_refreshments"}:
                    plan.append(("refreshments", 1))
                if "hotel" in items and ent.hotel_nights_owed:
                    plan += [("hotel_night", ent.hotel_nights_owed), ("hotel_transport", 1)]
                for kind, qty in plan:
                    v = deps.tools.issue_care_voucher(
                        cid, kind, std[kind], f"{cid}:care:{kind}", qty
                    )
                    amounts.append(v.amount_eur)
                    actions.append(
                        f"issued {qty} x {kind.replace('_', ' ')} voucher of EUR {v.amount_eur}"
                        f" (ref {v.voucher_id})"
                    )
            if ent.refund_or_rerouting and REFUND_REQUEST.search(state["guard"]["clean_text"]):
                refund = deps.tools.initiate_refund(
                    booking.booking_ref,
                    booking.fare_eur,
                    booking.case.disruption_type,
                    f"{cid}:refund",
                )
                refund_amount = refund.amount_eur
                actions.append(
                    f"refund of EUR {refund.amount_eur} initiated (ref {refund.refund_id}), "
                    f"paid within {refund.due_within_days} days"
                )
        except ServiceError as exc:
            return {
                "actions": [*actions, f"action stopped: {exc}"],
                "voucher_amounts": amounts,
                "route": "handoff",
                "handoff_reason": "authority_exceeded",
            }
        return {
            "actions": actions,
            "voucher_amounts": amounts,
            "refund_amount": refund_amount,
            "route": "draft",
        }

    # ---------------------------------------------------------------- draft, checks, judge

    def draft(state: State) -> dict[str, Any]:
        ent = Entitlement.model_validate(state["entitlement"])
        triage = TriageResult.model_validate(state["triage"])
        booking = Booking.model_validate(state["booking"])
        ctx = state["context"]
        facts = drafting.facts_block(
            entitlement=ent,
            triage=triage,
            passenger_name=booking.passenger_name,
            loyalty_tier=booking.loyalty_tier,
            flight=ctx["flight"],
            actions=state.get("actions", []),
            rebooking=ctx.get("rebooking", []),
            playbook=ctx["playbook"],
            history={"repeat_contacts": ctx["repeat_contacts"]},
            injection=state["guard"]["injection"],
            abusive=state["guard"]["abusive"],
            scheduled_departure=booking.case.scheduled_departure,
        )
        prompt = drafting.user_prompt(facts, state.get("transcript", []), state.get("guidance", []))
        if deps.client is None:
            return {"route": "handoff", "handoff_reason": "llm_unavailable"}
        try:
            res = deps.client.text(
                drafting.ROLE,
                drafting.INSTRUCTIONS.format(max_words=cfg.agent.reply_max_words),
                prompt,
            )
        except LLMUnavailableError:
            return {"route": "handoff", "handoff_reason": "llm_unavailable"}
        attempts = state.get("attempts", [])
        attempt = DraftAttempt(
            attempt_id=uuid.uuid4().hex,
            conversation_id=state["conversation_id"],
            attempt_no=len(attempts) + 1,
            draft_text=res.value,
            tool_calls=list(deps.tools.calls),
            model=res.meta.model,
            created_at=deps.clock(),
        )
        events = _event(
            state, deps, "draft.created", {"attempt_no": attempt.attempt_no, "model": attempt.model}
        )
        if attempt.attempt_no > 1:
            events = _event(
                {**state, "events": events},
                deps,
                "revision.started",
                {"guidance": state.get("guidance", [])},
            )
        return {
            "attempts": [*attempts, attempt.model_dump(mode="json")],
            "events": events,
            "route": "checks",
        }

    def run_checks(state: State) -> dict[str, Any]:
        ent = Entitlement.model_validate(state["entitlement"])
        attempts = list(state["attempts"])
        last = dict(attempts[-1])
        inp = code_checks.CheckInputs(
            entitlement=ent,
            issued_voucher_amounts=set(state.get("voucher_amounts", [])),
            refund_amount=state.get("refund_amount"),
            care_expected=bool(ent.care) and state["context"]["days_until_departure"] == 0,
            allowed_timeline_days={ent.refund_or_rerouting.refund_within_days}
            if ent.refund_or_rerouting
            else {7},
        )
        results = code_checks.run_checks(last["draft_text"], inp)
        last["code_checks"] = [r.model_dump() for r in results]
        attempts[-1] = last
        failed = code_checks.failed(results)
        return {
            "attempts": attempts,
            "route": "judge",
            "events": _event(
                state, deps, "checks.completed", {"failed": [f.check_id for f in failed]}
            ),
        }

    def judge_node(state: State) -> dict[str, Any]:
        attempt = state["attempts"][-1]
        checks = [CheckResult.model_validate(c) for c in attempt["code_checks"]]
        verdict = deps.judge.evaluate(
            draft=attempt["draft_text"],
            transcript=state.get("transcript", []),
            triage=TriageResult.model_validate(state["triage"]),
            entitlement=Entitlement.model_validate(state["entitlement"]),
            checks=checks,
            target="reply",
        )
        failed = code_checks.failed(checks)
        # Decision by code: a failed code check always means revise/handoff, whatever the judge says
        wants_revision = bool(failed) or verdict.decision == "revise"
        revisions_left = len(state["attempts"]) <= cfg.agent.max_revisions
        if verdict.decision == "handoff" or verdict.tier_bumped_to == "S4":
            route, reason = "handoff", "judge_handoff"
        elif wants_revision and revisions_left:
            route, reason = "draft", None
        elif wants_revision:
            route, reason = "handoff", "revisions_exhausted"
        else:
            route, reason = "finish", None
        guidance = [f"{f.check_id}: {f.detail}" for f in failed] + verdict.revision_guidance
        out: dict[str, Any] = {
            "verdicts": [*state.get("verdicts", []), verdict.model_dump()],
            "guidance": guidance if route == "draft" else state.get("guidance", []),
            "route": route,
            "handoff_reason": reason,
            "events": _event(
                state,
                deps,
                "judgement.completed",
                {"decision": route, "code_check_failures": [f.check_id for f in failed]},
            ),
        }
        if route == "finish":
            out["reply"] = attempt["draft_text"]
        return out

    # ---------------------------------------------------------------- handoff and finish

    def handoff_node(state: State) -> dict[str, Any]:
        reason = state.get("handoff_reason") or "unspecified"
        triage = TriageResult.model_validate(state["triage"]) if state.get("triage") else None
        ent = Entitlement.model_validate(state["entitlement"]) if state.get("entitlement") else None
        tier = triage.tier if triage else "S2"
        sla = cfg.sla_minutes.get(tier, 60)
        ticket = state.get("ticket")
        if ticket is None:
            summary, _source = handoff.summarize(
                deps.client,
                transcript=state.get("transcript", []),
                triage=triage,
                entitlement=ent,
                actions=state.get("actions", []),
                reason=reason,
            )
            record = deps.tools.create_ticket(
                state["conversation_id"],
                tier,
                summary.summary,
                f"{state['conversation_id']}:handoff",
            )
            ticket = HandoffTicket(
                ticket_id=record.ticket_id,
                conversation_id=state["conversation_id"],
                tier=tier,
                urgency=triage.urgency if triage else "soon",
                summary=summary.summary,
                entitlement=ent,
                actions_tried=state.get("actions", []),
                passenger_state=summary.passenger_state,
                recommended_next_step=summary.recommended_next_step,
                reason=reason,
                sla_due_at=deps.clock() + timedelta(minutes=sla),
            ).model_dump(mode="json")
            events = _event(
                state,
                deps,
                "handoff.created",
                {
                    "ticket_id": record.ticket_id,
                    "tier": tier,
                    "urgency": ticket["urgency"],
                    "reason": reason,
                    "summary": ticket["summary"],
                    "sla_minutes": sla,
                    "sla_due_at": ticket["sla_due_at"],
                },
            )
        else:
            events = state.get("events", [])
        message = handoff.holding_message(reason, tier, sla)
        verdicts = list(state.get("verdicts", []))
        if triage is not None:
            # FR-9.2 / FR-9.3: the holding message and the ticket summary are judged too.
            checks = code_checks.run_checks(message, code_checks.CheckInputs(entitlement=ent))
            held = deps.judge.evaluate(
                draft=message,
                transcript=state.get("transcript", []),
                triage=triage,
                entitlement=ent,
                checks=checks,
                target="holding_message",
            )
            summary_verdict = deps.judge.evaluate(
                draft=ticket["summary"],
                transcript=state.get("transcript", []),
                triage=triage,
                entitlement=ent,
                checks=[],
                target="handoff_summary",
            )
            verdicts += [held.model_dump(), summary_verdict.model_dump()]
            if code_checks.failed(checks) or held.decision != "approve":
                message = handoff.holding_message("default", tier, sla)
        return {
            "ticket": ticket,
            "status": "handed_off",
            "reply": message,
            "events": events,
            "verdicts": verdicts,
            "route": "finish",
        }

    def finish(state: State) -> dict[str, Any]:
        incoming = state["incoming"] or {}
        reply = state.get("reply") or ""
        status = "handed_off" if state.get("status") == "handed_off" else "answered"
        ticket = state.get("ticket")
        outcome = {
            "reply_text": reply,
            "status": status,
            "ticket_id": ticket["ticket_id"] if ticket else None,
        }
        processed = {**state.get("processed", {}), incoming["idempotency_key"]: outcome}
        transcript = [
            *state.get("transcript", []),
            {"role": "agent", "text": reply, "at": deps.clock().isoformat()},
        ]
        booking = state.get("booking")
        if booking:
            deps.tools.history.record_contact(
                booking["passenger_ref"],
                state["conversation_id"],
                booking["booking_ref"],
                state.get("channel", "webhook"),
            )
        return {
            "outcome": outcome,
            "processed": processed,
            "transcript": transcript,
            "status": "handed_off" if status == "handed_off" else "active",
            "events": _event(state, deps, "reply.sent", {"status": status}),
        }

    # ---------------------------------------------------------------- wiring

    def by_route(state: State) -> str:
        return state["route"]

    g: StateGraph[Any, Any, Any, Any] = StateGraph(State)
    for name, fn in [
        ("intake", intake),
        ("guardrails", guard),
        ("redirect", redirect),
        ("context", context),
        ("ask_booking", ask_booking),
        ("triage", triage_node),
        ("entitlement", entitlement_node),
        ("care_actions", care_actions),
        ("draft", draft),
        ("checks", run_checks),
        ("judge", judge_node),
        ("handoff", handoff_node),
        ("finish", finish),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", by_route, {"end": END, "guardrails": "guardrails"})
    g.add_conditional_edges(
        "guardrails", by_route, {"handoff": "handoff", "redirect": "redirect", "context": "context"}
    )
    g.add_edge("redirect", "finish")
    g.add_conditional_edges(
        "context",
        by_route,
        {"triage": "triage", "ask_booking": "ask_booking", "handoff": "handoff"},
    )
    g.add_edge("ask_booking", "finish")
    g.add_edge("triage", "entitlement")
    g.add_conditional_edges(
        "entitlement", by_route, {"care_actions": "care_actions", "handoff": "handoff"}
    )
    g.add_conditional_edges("care_actions", by_route, {"draft": "draft", "handoff": "handoff"})
    g.add_conditional_edges("draft", by_route, {"checks": "checks", "handoff": "handoff"})
    g.add_edge("checks", "judge")
    g.add_conditional_edges(
        "judge", by_route, {"finish": "finish", "draft": "draft", "handoff": "handoff"}
    )
    g.add_edge("handoff", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
