"""Rule half of triage (FR-3.2): text cues plus what the system already knows.

Deliberately sensitive: a missed crisis is far worse than an over-escalation, and the merge takes
the higher of the rule and LLM tiers. Patterns are English-first and tolerant of non-native
phrasing ("my son is alone", "medicine finish soon").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from flightright.models import TIER_ORDER, Tier, Urgency, VulnerabilityFlag


class TriageContext(BaseModel):
    """Facts the system knows before reading the message (booking, history, flight status)."""

    disruption_type: str | None = None
    days_until_departure: int | None = None  # negative = past flight, 0 = today
    departure_delay_minutes: int = 0
    booking_flags: list[VulnerabilityFlag] = Field(default_factory=list)  # e.g. assistance booked
    repeat_contact_count: int = 0


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


S4_MEDICAL = _rx(
    r"\b(medication|medicine|medicines|insulin|inhaler|pills?|tablets?)\b.{0,60}\b(run(ning)?\s*out|finish|"
    r"left|last|need|without|empty)",
    r"\b(run(ning)?\s*out|out)\s+of\s+(my\s+)?(medication|medicine|insulin|pills)",
    r"chest pain",
    r"can'?t breathe|cannot breathe|trouble breathing",
    r"\bseizure",
    r"\bfaint(ed|ing)?\b",
    r"\bambulance\b",
    r"\b(diabetic|epilep\w+)\b.{0,40}\b(crisis|attack|low|need)",
)
S4_MINOR = _rx(
    r"\b(child|kid|son|daughter|boy|girl|minor|nephew|niece|grand(son|daughter))\b"
    r".{0,50}\b(alone|by "
    r"(him|her)self|on (his|her) own|unaccompanied|stranded|no one|nobody)",
    r"\bunaccompanied (minor|child)\b",
)
S4_SAFETY = _rx(
    r"\b(harass\w*|threaten\w*|attack\w*|assault\w*|follow(ed|ing) me|stalk\w*)\b",
    r"\b(feel|i am|i'm)\s+(very\s+)?(unsafe|in danger|scared for my (life|safety))",
)
S4_CRISIS = _rx(
    r"\b(kill myself|end it all|end my life|suicid\w*|self[- ]harm|don'?t want to live)\b",
    r"\b(panic attack|breaking down|can'?t cope|can'?t stop crying|falling apart)\b",
    r"\b(losing my mind|freaking out|(i'?m|i am) shaking|(completely|totally) panick\w*|"
    r"(i'?m|i am) panicking)\b",
)
VULNERABILITY = {
    "reduced_mobility": _rx(r"wheel\s?chair", r"reduced mobility", r"\bcrutches\b", r"can'?t walk"),
    "infant": _rx(r"\b(baby|infant|newborn|toddler)\b", r"\b\d{1,2}[- ]month[- ]old\b"),
    "pregnant": _rx(r"\bpregnan\w*", r"expecting a baby", r"\bweeks? along\b"),
    "medical_assistance": _rx(
        r"medical condition",
        r"\b(diabetic|diabetes|epilep\w*|heart condition|"
        r"disabilit\w*|chemo\w*)\b",
    ),
    "unaccompanied_minor": S4_MINOR,
}
# Bare "hotel", "tonight", "stranded" and "connection" are deliberately not signals: they appear in
# ordinary S2 messages ("do I get a hotel voucher?"); an explicit stay or time pressure is required.
OVERNIGHT = _rx(
    r"overnight",
    r"nowhere to (sleep|stay)",
    r"need (a )?(hotel|place to (sleep|stay))",
    r"sleep (at|in) the airport",
    r"stranded (here )?(for the night|until tomorrow)",
    r"spend the night",
    r"no place to stay",
)
DEADLINE = _rx(
    r"\bin (about |only |less than )?\d+\s*(min|minutes|hours?|h)\b",
    r"\b(wedding|funeral|interview|exam|surgery|appointment|cruise|last train|last bus)\b",
    r"\b(first thing|early) (tomorrow|in the morning)\b",
)
REPEAT = _rx(
    r"\bagain\b",
    r"(second|third|fourth|\d+(st|nd|rd|th)) time",
    r"already (contacted|written|"
    r"called|wrote|asked|reached out)",
    r"still (no|haven'?t|waiting for)",
    r"no (one|body) (has )?"
    r"(answered|replied|responded)",
    r"contacted (you|support) before",
)
DISTRESS = _rx(
    r"!!+",
    r"\b(furious|outraged|disgust\w*|unacceptable|ridiculous|worst|nightmare|"
    r"desperate|terrible|appalling|scandal\w*|incompetent|useless|pathetic|joke|disgrace\w*|"
    r"outrage\w*|dumped|shameful|insult\w*)\b",
    r"\bi (demand|insist)\b",
    r"\bpanick?\w*\b",
    r"\b(very|so|really|extremely) (angry|upset|mad)\b",
    r"kidding me",
    r"\bfix (this|it) (right )?now\b",
    r"\b(bad|terrible|not good|awful) service\b",
    r"\bno ?(one|body) (help|helps|is helping)\b",
    r"\bneed help (right )?now\b",
)
PAST = _rx(
    r"\blast (week|month|year)\b",
    r"\b\d+ (days|weeks|months) ago\b",
    r"\bcan i (still )?claim\b",
    r"\b(was|were) (delayed|cancelled|canceled)\b",
    r"\barrived .{0,30} late\b",
)


@dataclass
class RuleTriage:
    tier: Tier
    urgency: Urgency
    frustration: int
    flags: list[VulnerabilityFlag] = field(default_factory=list)
    deadline_minutes: int | None = None
    reasons: list[str] = field(default_factory=list)


def _caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


def _deadline_minutes(text: str) -> int | None:
    m = re.search(r"\bin (?:about |only |less than )?(\d+)\s*(min|minutes|hours?|h)\b", text, re.I)
    if not m:
        return None
    value = int(m.group(1))
    return value * 60 if m.group(2).lower().startswith("h") else value


def rule_triage(message: str, ctx: TriageContext) -> RuleTriage:
    reasons: list[str] = []
    flags: list[VulnerabilityFlag] = list(ctx.booking_flags)
    for flag, pattern in VULNERABILITY.items():
        if pattern.search(message) and flag not in flags:
            flags.append(flag)  # type: ignore[arg-type]

    tier: Tier = "S2"
    upcoming = ctx.days_until_departure is not None and ctx.days_until_departure > 0
    past = (ctx.days_until_departure is not None and ctx.days_until_departure < 0) or (
        PAST.search(message) is not None and not upcoming and ctx.days_until_departure != 0
    )
    if past or (
        upcoming and ctx.days_until_departure is not None and ctx.days_until_departure >= 2
    ):
        tier = "S1"
        reasons.append("past flight" if past else "flight is days away")

    def bump(new: Tier, why: str) -> None:
        nonlocal tier
        if TIER_ORDER[new] > TIER_ORDER[tier]:
            tier = new
        reasons.append(why)

    frustration = 2
    if DISTRESS.search(message) or _caps_ratio(message) > 0.3:
        frustration = 4
    if len(DISTRESS.findall(message)) >= 2 or _caps_ratio(message) > 0.5:
        frustration = 5

    if not past:
        if OVERNIGHT.search(message):
            bump("S3", "stranded / overnight")
        deadline = _deadline_minutes(message)
        if (deadline is not None and deadline <= 360) or DEADLINE.search(message):
            bump("S3", "time-critical deadline")
        if flags:
            bump("S3", f"vulnerability: {', '.join(flags)}")
        if frustration >= 5:
            bump("S3", "strong distress")
    repeat = max(
        ctx.repeat_contact_count,
        2
        if REPEAT.search(message)
        and re.search(r"(third|fourth|\d+(rd|th)) time|already .* (twice|two times)", message, re.I)
        else 0,
    )
    if repeat >= 2:
        bump("S3", f"repeat contact ({repeat})")

    for pattern, why in (
        (S4_MEDICAL, "urgent medical need"),
        (S4_MINOR, "child alone / stranded"),
        (S4_SAFETY, "personal safety"),
        (S4_CRISIS, "acute crisis"),
    ):
        if pattern.search(message):
            bump("S4", why)

    deadline = _deadline_minutes(message)
    urgency: Urgency = "none"
    if tier == "S4" or (deadline is not None and deadline <= 240 and not past):
        urgency = "immediate"
    elif tier == "S3" or (ctx.days_until_departure == 0 and not past):
        urgency = "soon"
    return RuleTriage(
        tier=tier,
        urgency=urgency,
        frustration=frustration,
        flags=flags,
        deadline_minutes=deadline if not past else None,
        reasons=reasons,
    )
