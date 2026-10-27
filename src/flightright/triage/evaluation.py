"""Triage metrics (SPEC §10.2): confusion matrix, recall@S3+, per-tier precision / recall."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from flightright.models import TIER_ORDER, Scenario
from flightright.triage.rules import TriageContext

TIERS = ["S1", "S2", "S3", "S4"]


def context_for(s: Scenario) -> TriageContext:
    """What the live system would know for this scenario: booking, history, flight status."""
    timing = s.situation.timing
    days = (
        -7 if timing == "past" else 0 if timing == "at_airport" else (s.case.notice_given_days or 3)
    )
    booked = [
        f
        for f in s.case.passenger_flags
        if f in ("reduced_mobility", "infant", "unaccompanied_minor")
    ]
    return TriageContext(
        disruption_type=s.case.disruption_type,
        days_until_departure=days,
        departure_delay_minutes=s.case.departure_delay_minutes if timing == "at_airport" else 0,
        booking_flags=booked,  # type: ignore[arg-type]
        repeat_contact_count=s.situation.repeat_contact_count,
    )


@dataclass
class TriageMetrics:
    n: int = 0
    confusion: dict[str, Counter[str]] = field(
        default_factory=lambda: {t: Counter() for t in TIERS}
    )

    def add(self, gold: str, predicted: str) -> None:
        self.n += 1
        self.confusion[gold][predicted] += 1

    @property
    def accuracy(self) -> float:
        return sum(self.confusion[t][t] for t in TIERS) / self.n if self.n else 0.0

    @property
    def recall_s3_plus(self) -> float:
        """Share of truly severe cases (S3/S4) predicted as S3 or S4."""
        severe = sum(sum(self.confusion[g].values()) for g in ("S3", "S4"))
        caught = sum(self.confusion[g][p] for g in ("S3", "S4") for p in ("S3", "S4"))
        return caught / severe if severe else 1.0

    @property
    def under_triage_rate(self) -> float:
        under = sum(
            c for g in TIERS for p, c in self.confusion[g].items() if TIER_ORDER[p] < TIER_ORDER[g]
        )
        return under / self.n if self.n else 0.0

    @property
    def over_triage_rate(self) -> float:
        over = sum(
            c for g in TIERS for p, c in self.confusion[g].items() if TIER_ORDER[p] > TIER_ORDER[g]
        )
        return over / self.n if self.n else 0.0

    def recall(self, tier: str) -> float:
        total = sum(self.confusion[tier].values())
        return self.confusion[tier][tier] / total if total else 0.0

    def precision(self, tier: str) -> float:
        predicted = sum(self.confusion[g][tier] for g in TIERS)
        return self.confusion[tier][tier] / predicted if predicted else 0.0

    def table(self) -> str:
        head = (
            "| gold \\ predicted | "
            + " | ".join(TIERS)
            + " | recall |\n|---|"
            + "---|" * (len(TIERS) + 1)
        )
        rows = [
            f"| **{g}** | "
            + " | ".join(str(self.confusion[g][p]) for p in TIERS)
            + f" | {self.recall(g):.0%} |"
            for g in TIERS
        ]
        prec = "| precision | " + " | ".join(f"{self.precision(p):.0%}" for p in TIERS) + " | |"
        return "\n".join([head, *rows, prec])
