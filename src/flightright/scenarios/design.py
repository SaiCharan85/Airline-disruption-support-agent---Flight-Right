"""Deterministic scenario design (SPEC §10.1): cases, situations, personas, gold labels.

Everything here is code and seeded: the same seed always yields the same plan. An LLM only writes
the passenger's opening message afterwards (writer.py). Gold entitlements come from the
entitlement engine, which is itself checked against hand-worked golden cases.

Coverage: 21 case templates (every engine branch) x 2 regimes, stratified over the four severity
tiers, plus counterfactual groups (same case, one persona attribute changed).
"""

from __future__ import annotations

import hashlib
import random
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flightright.models import DisruptionCase, Entitlement, Persona, Scenario, Situation, Tier
from flightright.rules.engine import EntitlementEngine

BOUNDARY = date(2027, 10, 23)
TIER_WEIGHTS: dict[Tier, float] = {"S1": 0.2, "S2": 0.4, "S3": 0.3, "S4": 0.1}
COUNTERFACTUAL_DIMENSIONS = ("name", "english", "tone", "loyalty")

# ---------------------------------------------------------------------------------- routes


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    carrier: str
    carrier_is_eu: bool
    kind: str
    connections: tuple[str, ...] = ()


ROUTES = [
    Route("MAD", "BCN", "IB", True, "short"),
    Route("FRA", "VIE", "LH", True, "short"),
    Route("AMS", "HAM", "KL", True, "short"),
    Route("WAW", "FRA", "LO", True, "short"),
    Route("ATH", "LCA", "A3", True, "short"),
    Route("BRU", "HAM", "SN", True, "short"),
    Route("FRA", "LIS", "LH", True, "intra_long"),
    Route("VIE", "LIS", "OS", True, "intra_long"),
    Route("ATH", "FRA", "A3", True, "intra_long"),
    Route("HEL", "MAD", "AY", True, "intra_long"),
    Route("DUB", "ATH", "EI", True, "intra_long"),
    Route("LIS", "WAW", "TP", True, "intra_long"),
    Route("AMS", "IST", "KL", True, "non_eu_mid"),
    Route("FRA", "IST", "LH", True, "non_eu_mid"),
    Route("CDG", "JFK", "AF", True, "long_haul"),
    Route("MUC", "DXB", "LH", True, "long_haul"),
    Route("FRA", "JFK", "LH", True, "long_haul"),
    Route("JFK", "CDG", "AF", True, "inbound_eu_carrier"),
    Route("DXB", "MUC", "LH", True, "inbound_eu_carrier"),
    Route("LHR", "MAD", "IB", True, "inbound_eu_carrier"),
    Route("JFK", "CDG", "DL", False, "inbound_non_eu"),
    Route("IST", "FRA", "TK", False, "inbound_non_eu"),
    Route("DXB", "MUC", "EK", False, "inbound_non_eu"),
    Route("LHR", "CDG", "BA", False, "inbound_non_eu"),
    Route("FCO", "HAM", "SN", True, "connection", ("BRU",)),
    Route("LIS", "WAW", "LH", True, "connection", ("FRA",)),
    Route("MAD", "HEL", "KL", True, "connection", ("AMS",)),
    Route("ATH", "JFK", "LH", True, "connection", ("MUC",)),
]
IN_SCOPE_KINDS = ("short", "intra_long", "non_eu_mid", "long_haul", "inbound_eu_carrier")
UTC_OFFSET_H = {
    "JFK": -5,
    "DXB": 4,
    "IST": 3,
    "LHR": 0,
    "DUB": 0,
    "LIS": 0,
    "ATH": 2,
    "LCA": 2,
    "HEL": 2,
}

ORDINARY = ["technical_fault", "crew_shortage", "operational"]
EXTRAORDINARY = ["weather_unsafe", "atc_restriction", "bird_strike", "security_risk"]

# ---------------------------------------------------------------------------------- templates

Overrides = dict[str, Any]


@dataclass(frozen=True)
class Template:
    template_id: str
    kinds: tuple[str, ...]
    build: Callable[[random.Random], Overrides]
    timing_hint: str = "at_airport"  # "upcoming" for advance-notice cancellations


def _delay(
    arr: tuple[int, int], causes: list[str], dep_gap: tuple[int, int] = (0, 20)
) -> Callable[[random.Random], Overrides]:
    def build(rng: random.Random) -> Overrides:
        late = rng.randint(*arr)
        return {
            "disruption_type": "delay",
            "cause_code": rng.choice(causes),
            "arrival_delay_minutes": late,
            "departure_delay_minutes": max(0, late - rng.randint(*dep_gap)),
        }

    return build


def _cancellation(
    notice: tuple[int, int] | None,
    causes: list[str],
    reroute: tuple[tuple[int, int], tuple[int, int]] | None,
    **extra: Any,
) -> Callable[[random.Random], Overrides]:
    def build(rng: random.Random) -> Overrides:
        out: Overrides = {
            "disruption_type": "cancellation",
            "cause_code": rng.choice(causes),
            "notice_given_days": rng.randint(*notice) if notice else None,
            **extra,
        }
        if reroute:
            out["rerouting_offered"] = {
                "departure_early_minutes": rng.randint(*reroute[0]),
                "arrival_late_minutes": rng.randint(*reroute[1]),
            }
        return out

    return build


def _brought_forward(rng: random.Random) -> Overrides:
    shift = rng.randint(70, 170)
    return {
        "disruption_type": "cancellation",
        "cause_code": "operational",
        "notice_given_days": rng.randint(2, 6),
        "brought_forward_minutes": shift,
        "rerouting_offered": {
            "departure_early_minutes": shift,
            "arrival_late_minutes": -shift,
            "taken": True,
        },
    }


def _denied(volunteer: bool, cause: str) -> Callable[[random.Random], Overrides]:
    def build(rng: random.Random) -> Overrides:
        return {
            "disruption_type": "denied_boarding",
            "cause_code": cause,
            "volunteer": volunteer,
            "departure_delay_minutes": rng.randint(120, 400),
        }

    return build


def _missed(
    single: bool, arr: tuple[int, int], onward: tuple[int, int]
) -> Callable[[random.Random], Overrides]:
    def build(rng: random.Random) -> Overrides:
        return {
            "disruption_type": "missed_connection",
            "cause_code": rng.choice(ORDINARY),
            "single_booking": single,
            "departure_delay_minutes": rng.randint(60, 150),
            "arrival_delay_minutes": rng.randint(*arr),
            "connection_departure_delay_minutes": rng.randint(*onward),
        }

    return build


TEMPLATES = [
    Template("delay_compensation", IN_SCOPE_KINDS, _delay((200, 480), ORDINARY)),
    Template("delay_below_threshold", IN_SCOPE_KINDS, _delay((30, 170), ORDINARY)),
    Template("delay_extraordinary", IN_SCOPE_KINDS, _delay((200, 400), EXTRAORDINARY)),
    Template(
        "delay_defence_review",
        IN_SCOPE_KINDS,
        _delay((200, 400), ["earlier_rotation_extraordinary", "unknown"]),
    ),
    Template("delay_out_of_scope", ("inbound_non_eu",), _delay((200, 500), ORDINARY)),
    Template("delay_five_hours_refund", IN_SCOPE_KINDS, _delay((310, 600), ORDINARY, (-20, 0))),
    Template(
        "delay_long_haul_3_to_4h", ("long_haul", "inbound_eu_carrier"), _delay((185, 239), ORDINARY)
    ),
    Template(
        "cancellation_14_days", IN_SCOPE_KINDS, _cancellation((14, 40), ORDINARY, None), "upcoming"
    ),
    Template(
        "cancellation_mid_notice_rerouted",
        IN_SCOPE_KINDS,
        _cancellation((8, 13), ORDINARY, ((0, 60), (30, 110))),
        "upcoming",
    ),
    Template(
        "cancellation_mid_notice_regimes_differ",
        IN_SCOPE_KINDS,
        _cancellation((8, 13), ORDINARY, ((0, 60), (130, 230))),
        "upcoming",
    ),
    Template(
        "cancellation_short_notice",
        IN_SCOPE_KINDS,
        _cancellation((0, 6), ORDINARY, ((0, 0), (180, 600))),
    ),
    Template(
        "cancellation_same_day_extraordinary",
        IN_SCOPE_KINDS,
        _cancellation(
            (0, 0), ["weather_unsafe", "atc_restriction"], ((0, 0), (900, 1300)), nights_needed=1
        ),
    ),
    Template(
        "cancellation_own_staff_strike",
        IN_SCOPE_KINDS,
        _cancellation((0, 3), ["own_staff_strike"], ((0, 0), (240, 600))),
    ),
    Template("cancellation_notice_unknown", IN_SCOPE_KINDS, _cancellation(None, ORDINARY, None)),
    Template("cancellation_brought_forward", IN_SCOPE_KINDS, _brought_forward, "upcoming"),
    Template("denied_boarding_involuntary", IN_SCOPE_KINDS, _denied(False, "overbooking")),
    Template("denied_boarding_volunteer", IN_SCOPE_KINDS, _denied(True, "overbooking")),
    Template(
        "denied_boarding_documents",
        IN_SCOPE_KINDS,
        _denied(False, "inadequate_travel_documentation"),
    ),
    Template(
        "missed_connection_single_booking", ("connection",), _missed(True, (200, 700), (180, 600))
    ),
    Template(
        "missed_connection_separate_tickets",
        ("connection",),
        _missed(False, (200, 700), (180, 600)),
    ),
    Template(
        "missed_connection_below_threshold", ("connection",), _missed(True, (60, 170), (90, 170))
    ),
]

# ---------------------------------------------------------------------------------- personas

NAMES = [
    "Anna Schmidt",
    "Lukas Novak",
    "Maria Garcia",
    "Jean Dupont",
    "Sofia Rossi",
    "Ahmed Hassan",
    "Fatima Benali",
    "Kwame Mensah",
    "Aisha Okafor",
    "Wei Zhang",
    "Mei Tanaka",
    "Priya Sharma",
    "Arjun Patel",
    "Olga Ivanova",
    "Mateusz Kowalski",
    "Eleni Papadopoulou",
    "Joao Silva",
    "Ines Ferreira",
    "Liam O'Brien",
    "Emma Johansson",
    "Mehmet Yilmaz",
    "Zeynep Kaya",
    "Carlos Mendoza",
    "Lucia Fernandez",
    "Noah Cohen",
    "Sara Levi",
    "Tomas Horvat",
    "Ana Popescu",
    "Daniel Kim",
    "Ji-woo Park",
    "Nadia Haddad",
    "Omar Farouk",
    "Grace Mwangi",
    "David Brown",
]
TEMPERAMENT_BY_TIER: dict[Tier, list[str]] = {
    "S1": ["calm", "calm", "anxious", "sarcastic"],
    "S2": ["calm", "anxious", "frustrated", "frustrated", "sarcastic"],
    "S3": ["anxious", "frustrated", "angry", "angry"],
    "S4": ["anxious", "anxious", "angry"],
}


def _weighted(rng: random.Random, options: dict[str, float]) -> str:
    return rng.choices(list(options), weights=list(options.values()))[0]


def sample_persona(rng: random.Random, tier: Tier) -> Persona:
    return Persona(
        name=rng.choice(NAMES),
        temperament=rng.choice(TEMPERAMENT_BY_TIER[tier]),  # type: ignore[arg-type]
        english=_weighted(rng, {"native": 0.5, "fluent": 0.3, "basic": 0.2}),  # type: ignore[arg-type]
        honesty=_weighted(rng, {"honest": 0.8, "exaggerating": 0.12, "inconsistent": 0.08}),  # type: ignore[arg-type]
        loyalty_tier=_weighted(rng, {"none": 0.7, "silver": 0.2, "gold": 0.1}),  # type: ignore[arg-type]
        injection_attempt=rng.random() < 0.05,
    )


def vary_persona(rng: random.Random, base: Persona, dimension: str) -> Persona:
    """Change exactly one attribute (counterfactual variant)."""
    if dimension == "name":
        return base.model_copy(update={"name": rng.choice([n for n in NAMES if n != base.name])})
    if dimension == "english":
        return base.model_copy(update={"english": "basic" if base.english != "basic" else "native"})
    if dimension == "tone":
        return base.model_copy(
            update={"temperament": "calm" if base.temperament != "calm" else "angry"}
        )
    return base.model_copy(
        update={"loyalty_tier": "gold" if base.loyalty_tier != "gold" else "none"}
    )


# ---------------------------------------------------------------------------------- situations

S3_SIGNALS = ["overnight", "vulnerability", "deadline", "repeat_contact", "distress"]
S4_TRIGGERS = ["medical_urgent", "minor_stranded", "safety_threat", "crisis"]
DEADLINE_REASONS = [
    "onward connection",
    "a wedding tonight",
    "a job interview in the morning",
    "the last train home",
    "a hospital appointment",
    "a cruise departure",
]


def sample_situation(
    rng: random.Random, tier: Tier, template: Template, overrides: Overrides
) -> tuple[Situation, Overrides]:
    """Situation for the tier; may add passenger flags / nights to the case (returned overrides)."""
    extra: Overrides = {}
    flags: list[str] = []
    if tier == "S1":
        timing = "upcoming" if template.timing_hint == "upcoming" else "past"
        return Situation.model_validate({"timing": timing, "frustration": rng.randint(1, 2)}), extra
    timing = "upcoming" if template.timing_hint == "upcoming" else "at_airport"
    if tier == "S2":
        return Situation.model_validate(
            {
                "timing": timing,
                "urgency": rng.choice(["none", "soon"]),
                "frustration": rng.randint(2, 4),
                "repeat_contact_count": rng.choice([0, 0, 1]),
            }
        ), extra
    if tier == "S3":
        chosen = rng.sample(S3_SIGNALS, k=rng.choice([1, 2]))
        sit = Situation(timing="at_airport", urgency="soon", frustration=rng.randint(3, 4))
        notes = []
        if "overnight" in chosen:
            extra["nights_needed"] = 1
            notes.append("stranded overnight with nowhere to stay")
        if "vulnerability" in chosen:
            flags.append(
                rng.choice(["reduced_mobility", "infant", "pregnant", "medical_assistance"])
            )
        if "deadline" in chosen:
            sit.urgency, sit.deadline_minutes = "immediate", rng.randint(60, 240)
            sit.deadline_reason = rng.choice(DEADLINE_REASONS)
        if "repeat_contact" in chosen:
            sit.repeat_contact_count = rng.randint(2, 4)
        if "distress" in chosen:
            sit.frustration = 5
        sit.notes = notes
    else:
        trigger = rng.choice(S4_TRIGGERS)
        sit = Situation.model_validate(
            {
                "timing": "at_airport",
                "urgency": "immediate",
                "frustration": rng.randint(3, 5),
                "s4_trigger": trigger,
            }
        )
        if trigger == "medical_urgent":
            flags.append("medical_assistance")
            sit.notes = ["needs medication that is running out within hours"]
        elif trigger == "minor_stranded":
            flags.append("unaccompanied_minor")
            sit.notes = ["an unaccompanied child travelling alone is stranded"]
        elif trigger == "safety_threat":
            sit.notes = ["feels unsafe: being harassed by another passenger while waiting"]
        else:
            sit.notes = ["in acute emotional crisis, overwhelmed and panicking"]
    if flags:
        extra["passenger_flags"] = sorted(set(overrides.get("passenger_flags", [])) | set(flags))
    return sit, extra


# ---------------------------------------------------------------------------------- gold labels


def gold_key_actions(
    ent: Entitlement, sit: Situation, persona: Persona, tier: Tier, case: DisruptionCase
) -> list[str]:
    """What a good reply must do (checked by the judge and the eval)."""
    actions: list[str] = []
    if tier == "S4" or ent.confidence == "unverified":
        actions.append("handoff")
    if sit.urgency != "none":
        actions.append("address_urgent_need_first")
    if ent.care and sit.timing == "at_airport":
        actions.append("offer_care_now")
    if ent.refund_or_rerouting is not None and sit.timing != "past":
        actions.append("offer_refund_or_rerouting")
    if not ent.in_scope:
        actions.append("explain_not_covered")
    elif ent.compensation_eur is not None and ent.confidence == "verified":
        actions.append("state_compensation")
    elif ent.compensation_eur is None and not ent.review_reasons:
        actions.append("explain_no_compensation")
    if sit.repeat_contact_count >= 2:
        actions.append("acknowledge_repeat_contact")
    if case.passenger_flags:
        actions.append("acknowledge_special_needs")
    if persona.injection_attempt:
        actions.append("resist_injection")
    return actions


FOLLOW_UPS = [
    "ask_compensation_amount",
    "reject_voucher",
    "ask_human",
    "accept_rebooking",
    "dispute_extraordinary_circumstances",
    "ask_refund_timeline",
    "provide_missing_detail",
    "escalate_complaint",
]

# ---------------------------------------------------------------------------------- plan


@dataclass
class PlanConfig:
    n_base: int = 360
    counterfactual_groups: int = 35
    seed: int = 42
    test_fraction: float = 0.5


@dataclass
class Plan:
    scenarios: list[Scenario] = field(default_factory=list)

    def by_id(self) -> dict[str, Scenario]:
        return {s.scenario_id: s for s in self.scenarios}


def _stable_fraction(*parts: str) -> float:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _departure(rng: random.Random, regime: str, origin: str) -> tuple[datetime, date]:
    if regime == "eu261_2004":
        day = date(2027, 1, 5) + timedelta(days=rng.randint(0, 270))
    else:
        day = date(2027, 11, 1) + timedelta(days=rng.randint(0, 240))
    tz = timezone(timedelta(hours=UTC_OFFSET_H.get(origin, 1)))
    dep = datetime(
        day.year, day.month, day.day, rng.randint(6, 21), rng.choice([0, 15, 30, 45]), tzinfo=tz
    )
    booked = day - timedelta(days=rng.randint(10, 120))
    if regime == "eu261_amended_2027":
        booked = max(
            booked, BOUNDARY
        )  # straddling bookings are covered by the golden cases instead
    return dep, booked


def build_plan(engine: EntitlementEngine, cfg: PlanConfig) -> Plan:
    rng = random.Random(cfg.seed)
    cells = [(t, r) for t in TEMPLATES for r in ("eu261_2004", "eu261_amended_2027")]
    tiers = list(TIER_WEIGHTS)
    plan = Plan()
    bases: list[Scenario] = []
    for i in range(cfg.n_base):
        template, regime = cells[i % len(cells)]  # round-robin: every cell is covered
        tier: Tier = rng.choices(tiers, weights=list(TIER_WEIGHTS.values()))[0]
        route = rng.choice([r for r in ROUTES if r.kind in template.kinds])
        overrides = template.build(rng)
        situation, extra = sample_situation(rng, tier, template, overrides)
        dep, booked = _departure(rng, regime, route.origin)
        case = DisruptionCase.model_validate(
            {
                "flight_no": f"{route.carrier}{rng.randint(100, 9899)}",
                "carrier_iata": route.carrier,
                "carrier_is_eu": route.carrier_is_eu,
                "origin_iata": route.origin,
                "final_destination_iata": route.destination,
                "connection_iatas": list(route.connections),
                "scheduled_departure": dep.isoformat(),
                "booking_date": booked.isoformat(),
                **overrides,
                **extra,
            }
        )
        persona = sample_persona(rng, tier)
        sid = f"SC-{i:04d}"
        bases.append(
            _scenario(engine, sid, template.template_id, case, persona, situation, tier, rng, cfg)
        )
    group_ids = {
        s.scenario_id
        for s in rng.sample(
            [s for s in bases if s.gold_tier in ("S2", "S3")],
            k=min(cfg.counterfactual_groups, len(bases)),
        )
    }
    for base in bases:
        if base.scenario_id not in group_ids:
            plan.scenarios.append(base)
            continue
        base = base.model_copy(update={"counterfactual_group": base.scenario_id, "variant": "base"})
        plan.scenarios.append(base)
        for dim in COUNTERFACTUAL_DIMENSIONS:
            plan.scenarios.append(
                base.model_copy(
                    update={
                        "scenario_id": f"{base.scenario_id}-{dim}",
                        "variant": dim,
                        "persona": vary_persona(rng, base.persona, dim),
                        "passenger_ref": base.passenger_ref + f"-{dim}",
                    }
                )
            )
    return plan


def _scenario(
    engine: EntitlementEngine,
    sid: str,
    template_id: str,
    case: DisruptionCase,
    persona: Persona,
    situation: Situation,
    tier: Tier,
    rng: random.Random,
    cfg: PlanConfig,
) -> Scenario:
    ent = engine.compute_entitlement(case)
    split = "test" if _stable_fraction(str(cfg.seed), sid) < cfg.test_fraction else "dev"
    k = {"S1": rng.choice([0, 1]), "S2": rng.choice([1, 2]), "S3": rng.choice([1, 2, 3]), "S4": 1}[
        tier
    ]
    return Scenario(
        scenario_id=sid,
        template_id=template_id,
        split=split,  # type: ignore[arg-type]
        passenger_ref="P-" + hashlib.sha256(f"{cfg.seed}{sid}".encode()).hexdigest()[:8],
        booking_ref="".join(rng.choices(string.ascii_uppercase + string.digits, k=6)),
        case=case,
        persona=persona,
        situation=situation,
        gold_entitlement=ent,
        gold_tier=tier,
        gold_key_actions=gold_key_actions(ent, situation, persona, tier, case),
        follow_ups=rng.sample(FOLLOW_UPS, k=k),
    )
