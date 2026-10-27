"""Handoff (FR-9.2, FR-9.3): holding message, ticket summary and the HandoffTicket record."""

from __future__ import annotations

from pydantic import BaseModel

from flightright.llm.client import LLMClient
from flightright.llm.types import LLMUnavailableError
from flightright.models import Entitlement, TriageResult

ROLE = "summary"


class HandoffSummary(BaseModel):
    summary: str
    passenger_state: str
    recommended_next_step: str


SUMMARY_INSTRUCTIONS = (
    "You write handoff notes for a human airline support agent taking over a conversation. Be factual and "
    "brief: what happened, what the passenger needs now, what has been done, what is still open. Use only "
    "the facts given; never add amounts or promises. recommended_next_step is one concrete action."
)


def holding_message(reason: str, tier: str, sla_minutes: int) -> str:
    """Short message sent while a human takes over; checked and judged like any reply (FR-9.2)."""
    when = f"within {sla_minutes} minutes" if sla_minutes < 120 else "as soon as possible today"
    if tier == "S4":
        return (
            "I'm sorry you're going through this. I've passed your case to a member of our team as a "
            f"priority, and they will contact you {when}. If you need urgent medical help or feel unsafe, "
            "please speak to airport staff or call the emergency number 112 right away."
        )
    if reason == "human_requested":
        return (
            f"Of course. I've passed your conversation to a colleague, who will reply {when}. Everything "
            "you've told me is in the ticket, so you won't need to repeat it."
        )
    if reason == "needs_review":
        return (
            "Thank you for the details. Your case needs a check by a colleague before I can confirm what "
            f"you're entitled to. They will reply {when}, and your care and rebooking options are not affected."
        )
    if reason == "no_booking":
        return (
            "I couldn't find your booking from the details so far. Could you send your six-character "
            "booking reference? If you prefer, a colleague can help instead."
        )
    return (
        "I want to make sure this is handled properly, so I've passed your case to a colleague. They will "
        f"reply {when}."
    )


def summarize(
    client: LLMClient | None,
    *,
    transcript: list[dict[str, str]],
    triage: TriageResult | None,
    entitlement: Entitlement | None,
    actions: list[str],
    reason: str,
) -> tuple[HandoffSummary, str]:
    """Returns (summary, source) where source is the model key or 'fallback'."""
    last = transcript[-1]["text"] if transcript else ""
    facts = [
        f"handoff reason: {reason}",
        f"severity: {triage.tier if triage else 'unknown'}; urgency: {triage.urgency if triage else 'unknown'}",
        f"triage reasons: {'; '.join(triage.reasons[:6]) if triage else 'n/a'}",
        f"entitlement: {entitlement.compensation_reason if entitlement else 'not computed'}; "
        f"compensation {entitlement.compensation_eur if entitlement else 'n/a'}; "
        f"confidence {entitlement.confidence if entitlement else 'n/a'}",
        f"actions taken: {'; '.join(actions) or 'none'}",
        "conversation:",
        *(f"{t['role']}: {t['text']}" for t in transcript[-6:]),
    ]
    if client is not None:
        try:
            res = client.structured(ROLE, SUMMARY_INSTRUCTIONS, "\n".join(facts), HandoffSummary)
            return res.value, res.meta.model
        except LLMUnavailableError:
            pass
    return HandoffSummary(
        summary=f"Handoff ({reason}). Last passenger message: {last[:300]}",
        passenger_state=(
            f"tier {triage.tier}, urgency {triage.urgency}, frustration {triage.frustration}/5"
            if triage
            else "unknown"
        ),
        recommended_next_step="Contact the passenger, confirm their situation and resolve the open request.",
    ), "fallback"
