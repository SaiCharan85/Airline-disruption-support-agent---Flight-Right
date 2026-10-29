"""Planted errors for airline replies (SPEC §10.1), as JudgeKit error types.

Each error type takes a good reply and plants exactly one known defect, naming the rubric dimensions
that should catch it. Samples carry the reply plus a little metadata as text fields:

    Sample(id=..., fields={"reply": ..., "regime": "eu261_2004", "urgency": "immediate",
                           "repeat_contacts": "2"})

Mutators return None when an error cannot be planted in a sample (e.g. no amount to change).
"""

from __future__ import annotations

import random
import re

from judgekit import ErrorType, Mutation, Sample

AMOUNT = re.compile(r"(€\s?|EUR\s?)(\d{2,4})", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
CARE = re.compile(r"\b(meal|food|refreshment|drink|snack|hotel|accommodation|voucher)", re.I)
REPEAT = re.compile(
    r"\b(again|earlier|previous(ly)?|already|contacted|second|third|before)\b", re.I
)


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE.split(text.strip()) if s]


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def wrong_amount(s: Sample, rng: random.Random) -> Mutation | None:
    """Misstate what the passenger is owed: change the largest amount (the compensation)."""
    reply = s.fields["reply"]
    matches = list(AMOUNT.finditer(reply))
    if not matches:
        return None
    m = max(matches, key=lambda x: int(x.group(2)))
    old = int(m.group(2))
    new = rng.choice([v for v in (250, 300, 400, 600, 800) if v != old])
    return Mutation(
        changes={"reply": reply[: m.start(2)] + str(new) + reply[m.end(2) :]},
        description=f"amount {old} -> {new}",
    )


def wrong_regime(s: Sample, rng: random.Random) -> Mutation | None:
    claim = (
        "These rights come from the amended EU261 rules (Regulation 2026/2202)."
        if s.fields.get("regime") == "eu261_2004"
        else "These rights come from the original 2004 rules, which give two weeks' notice tiers."
    )
    return Mutation(
        changes={"reply": s.fields["reply"].rstrip() + " " + claim},
        description="wrong regime cited",
    )


def fake_extraordinary(s: Sample, rng: random.Random) -> Mutation | None:
    claim = (
        "Unfortunately the disruption was caused by extraordinary circumstances, so no "
        "compensation is due."
    )
    return Mutation(
        changes={"reply": s.fields["reply"].rstrip() + " " + claim},
        description="invented extraordinary circumstances",
    )


def voucher_push(s: Sample, rng: random.Random) -> Mutation | None:
    reply = s.fields["reply"]
    kept = [
        x
        for x in _sentences(reply)
        if not re.search(r"\b(refund|cash|bank transfer|money)\b", x, re.I)
    ]
    if len(kept) == len(_sentences(reply)):
        kept.append("")
    kept.append("We recommend accepting a travel voucher for a future flight instead.")
    return Mutation(
        changes={"reply": " ".join(x for x in kept if x)},
        description="travel voucher pushed, cash right removed",
    )


def missing_care(s: Sample, rng: random.Random) -> Mutation | None:
    sentences = _sentences(s.fields["reply"])
    kept = [x for x in sentences if not CARE.search(x)]
    if len(kept) == len(sentences) or not kept:
        return None
    return Mutation(changes={"reply": " ".join(kept)}, description="care obligations removed")


def buried_urgency(s: Sample, rng: random.Random) -> Mutation | None:
    if s.fields.get("urgency", "none") == "none":
        return None
    sentences = _sentences(s.fields["reply"])
    if len(sentences) < 4:
        return None
    filler = "Thank you for contacting us. We value every customer and take all feedback seriously."
    return Mutation(
        changes={"reply": " ".join([filler, *sentences[2:], *sentences[:2]])},
        description="time-critical help moved to the end",
    )


def cheerful_in_crisis(s: Sample, rng: random.Random) -> Mutation | None:
    sentences = [
        x
        for x in _sentences(s.fields["reply"])
        if not re.search(r"\b(sorry|apologi[sz]e)", x, re.I)
    ]
    opener = "Great news! We hope you're having a wonderful day despite this little hiccup!"
    return Mutation(
        changes={"reply": " ".join([opener, *sentences])}, description="cheerful tone in distress"
    )


def generic_apology(s: Sample, rng: random.Random) -> Mutation | None:
    sentences = _sentences(s.fields["reply"])
    if len(sentences) < 2:
        return None
    return Mutation(
        changes={"reply": " ".join(["We apologise for any inconvenience caused.", *sentences[1:]])},
        description="specific empathy replaced by a generic apology",
    )


def blame_shifting(s: Sample, rng: random.Random) -> Mutation | None:
    blame = "You should have checked your flight status before coming to the airport."
    sentences = _sentences(s.fields["reply"])
    return Mutation(
        changes={"reply": " ".join([*sentences[:1], blame, *sentences[1:]])},
        description="blames the passenger",
    )


def reask_info(s: Sample, rng: random.Random) -> Mutation | None:
    ask = "Before we can help, could you please send us your booking reference and flight number?"
    return Mutation(
        changes={"reply": s.fields["reply"].rstrip() + " " + ask},
        description="re-asks given details",
    )


def ignore_repeat_contact(s: Sample, rng: random.Random) -> Mutation | None:
    if int(s.fields.get("repeat_contacts", "0") or 0) < 1:
        return None
    sentences = _sentences(s.fields["reply"])
    kept = [x for x in sentences if not REPEAT.search(x)]
    if len(kept) == len(sentences) or not kept:
        return None
    return Mutation(
        changes={"reply": " ".join(kept)}, description="repeat contact no longer acknowledged"
    )


def partial_answer(s: Sample, rng: random.Random) -> Mutation | None:
    paragraphs = _paragraphs(s.fields["reply"])
    if len(paragraphs) < 3:
        return None
    return Mutation(
        changes={"reply": "\n\n".join([paragraphs[0], paragraphs[-1]])},
        description="middle of the answer removed",
    )


def no_next_step(s: Sample, rng: random.Random) -> Mutation | None:
    paragraphs = _paragraphs(s.fields["reply"])
    if len(paragraphs) < 2:
        return None
    return Mutation(
        changes={"reply": "\n\n".join(paragraphs[:-1])}, description="next step removed"
    )


ERROR_TYPES: list[ErrorType] = [
    ErrorType("wrong_amount", frozenset({"correctness"}), wrong_amount),
    ErrorType("wrong_regime", frozenset({"correctness"}), wrong_regime),
    ErrorType("fake_extraordinary", frozenset({"correctness"}), fake_extraordinary),
    ErrorType("voucher_push", frozenset({"correctness"}), voucher_push),
    ErrorType("missing_care", frozenset({"correctness", "relevance"}), missing_care),
    ErrorType("buried_urgency", frozenset({"urgency"}), buried_urgency),
    ErrorType("cheerful_in_crisis", frozenset({"tone"}), cheerful_in_crisis),
    ErrorType("generic_apology", frozenset({"tone", "grievance_handling"}), generic_apology),
    ErrorType("blame_shifting", frozenset({"grievance_handling", "tone"}), blame_shifting),
    ErrorType("reask_info", frozenset({"context_awareness"}), reask_info),
    ErrorType("ignore_repeat_contact", frozenset({"context_awareness"}), ignore_repeat_contact),
    ErrorType("partial_answer", frozenset({"relevance", "actionability"}), partial_answer),
    ErrorType("no_next_step", frozenset({"actionability"}), no_next_step),
]
