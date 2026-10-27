"""Guardrails (FR-2): injection, abuse, human requests, off-topic, PII minimization.

All deterministic. Injection never changes facts: entitlements come from the engine, so the worst an
injected instruction can do is change wording, which the code checks and the judge then catch.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|your|the)\b.{0,30}"
    r"\b(instructions?|rules?|guidelines?|prompt|policy|policies)\b"
    r"|\b(system prompt|developer mode|you are now|"
    r"act as (an? )?(admin|administrator|supervisor))\b"
    r"|\b(approve|issue|transfer|pay)\b.{0,30}\b(immediately|now|right away)\b.{0,40}"
    r"\b(\d[\d,.]*\s?(eur|euros?|€)|€\s?\d[\d,.]*)",
    re.IGNORECASE,
)
ABUSE = re.compile(
    r"\b(idiot|stupid|moron|imbecile|useless (bot|piece)|f+u+c+k+\w*|shit\w*|bastard|bitch|"
    r"screw you|go to hell|i will (find|hurt) you)\b",
    re.IGNORECASE,
)
HUMAN_REQUEST = re.compile(
    r"\b(speak|talk|chat)\b.{0,15}\b(to|with)\b.{0,15}\b(a |an |the )?(human|person|agent|someone|"
    r"manager|supervisor|representative|real person)\b"
    r"|\b(real|live) (person|human|agent)\b|\bhuman agent\b|\bcall me\b|\bescalate this\b",
    re.IGNORECASE,
)
TRAVEL_WORDS = re.compile(
    r"\b(flight|flights|fly|flying|delay\w*|cancel\w*|booking|book\w*|airport|gate|seat|board\w*|"
    r"refund\w*|compensat\w*|voucher|hotel|travel\w*|trip|plane|airline|connection|baggage|luggage|"
    r"ticket|reroute|rebook\w*|stranded|eu261|passenger|departure|arrival|landed|missed)\b",
    re.IGNORECASE,
)
BOOKING_REF = re.compile(r"\b(?=[A-Z0-9]{6}\b)(?=.*\d)(?=.*[A-Z])[A-Z0-9]{6}\b")
PASSPORT_OR_ID = re.compile(
    r"\b(passport|id card|identity card)\b[^.\n]{0,25}?\b([A-Z]{1,2}\d{6,9})\b", re.I
)
CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")


class GuardrailResult(BaseModel):
    injection: bool = False
    abusive: bool = False
    human_requested: bool = False
    off_topic: bool = False
    pii_masked: list[str] = Field(default_factory=list)
    clean_text: str
    booking_ref_in_text: str | None = None


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Never persist passport/ID or card numbers (FR-2.3)."""
    kinds: list[str] = []

    def _id(m: re.Match[str]) -> str:
        kinds.append("id_number")
        return m.group(0).replace(m.group(2), "[ID REMOVED]")

    def _card(m: re.Match[str]) -> str:
        kinds.append("card_number")
        return "[CARD REMOVED]"

    text = PASSPORT_OR_ID.sub(_id, text)
    text = CARD_NUMBER.sub(_card, text)
    return text, kinds


def check(text: str) -> GuardrailResult:
    clean, masked = mask_pii(text)
    ref = BOOKING_REF.search(clean)
    has_travel = bool(TRAVEL_WORDS.search(clean)) or ref is not None
    return GuardrailResult(
        injection=bool(INJECTION.search(clean)),
        abusive=bool(ABUSE.search(clean)),
        human_requested=bool(HUMAN_REQUEST.search(clean)),
        off_topic=not has_travel and len(clean.split()) >= 3,
        pii_masked=masked,
        clean_text=clean,
        booking_ref_in_text=ref.group(0) if ref else None,
    )


def mask_for_logs(text: str, booking_ref: str | None, name: str | None) -> str:
    """Booking references and names are masked in logs and ops responses by default (FR-2.3)."""
    if booking_ref:
        text = re.sub(re.escape(booking_ref), booking_ref[:2] + "****", text, flags=re.I)
    if name:
        for part in name.split():
            if len(part) > 2:
                text = re.sub(rf"\b{re.escape(part)}\b", part[0] + "***", text)
    return text
