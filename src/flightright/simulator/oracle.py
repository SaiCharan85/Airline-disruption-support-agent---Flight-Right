"""Handoff oracle (FR-13.2): resolves handoff tickets from the scenario's ground truth.

It plays the human agent who picks up a ticket. With `noise`, a share of resolutions is deliberately
wrong (e.g. compensation denied although owed), so downstream metrics can be tested against an
imperfect human, as in real operations. Deterministic for a given seed.
"""

from __future__ import annotations

import random
from datetime import datetime

from flightright.models import Resolution, Scenario


def resolve(scenario: Scenario, now: datetime, noise: float = 0.0, seed: int = 0) -> Resolution:
    rng = random.Random(f"{seed}:{scenario.scenario_id}")
    e = scenario.gold_entitlement
    if not e.in_scope:
        decision, paid, notes = (
            "not_covered",
            None,
            "Flight not covered by EU261; explained to the passenger.",
        )
    elif e.compensation_eur:
        decision, paid = "compensation_approved", e.compensation_eur
        notes = f"Compensation of EUR {e.compensation_eur} approved per {e.regime}."
    elif e.refund_or_rerouting:
        decision, paid, notes = (
            "rerouted_or_refunded",
            None,
            "Passenger rerouted or refunded; no compensation owed.",
        )
    else:
        decision, paid, notes = (
            "no_compensation",
            None,
            "No compensation owed; care provided where due.",
        )
    if noise and rng.random() < noise:
        if paid:
            decision, paid, notes = (
                "compensation_denied",
                None,
                "Denied (oracle noise: wrong decision).",
            )
        else:
            decision, paid, notes = (
                "goodwill_payment",
                100,
                "Goodwill payment (oracle noise: not owed).",
            )
    return Resolution(decision=decision, notes=notes, compensation_paid_eur=paid, resolved_at=now)
