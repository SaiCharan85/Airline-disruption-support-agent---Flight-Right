"""Core data models (SPEC §5). Names are normative; fields may grow."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Regime = Literal["eu261_2004", "eu261_amended_2027"]
DisruptionType = Literal["delay", "cancellation", "denied_boarding", "missed_connection"]
VulnerabilityFlag = Literal[
    "reduced_mobility",
    "disability",
    "accompanying_person",
    "service_dog",
    "infant",
    "unaccompanied_minor",
    "pregnant",
    "medical_assistance",
]
FareType = Literal[
    "public", "frequent_flyer", "free_of_charge", "non_public_reduced_fare", "staff_fare"
]
CareItem = Literal[
    "meals_refreshments",
    "refreshments",
    "refreshments_immediate",
    "meals",
    "communications",
    "hotel",
    "hotel_transport",
]


class ReroutingOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    departure_early_minutes: int = 0  # how much earlier than the original scheduled departure
    arrival_late_minutes: int = (
        0  # arrival delay at the final destination versus the original schedule
    )
    taken: bool = False


class DisruptionCase(BaseModel):
    """One passenger's disruption. Delays are whole minutes; arrival = door open / on-block."""

    model_config = ConfigDict(frozen=True)

    flight_no: str = ""
    carrier_iata: str
    carrier_is_eu: bool
    origin_iata: str
    destination_iata: str | None = None  # first-leg destination; defaults to the final destination
    final_destination_iata: str
    connection_iatas: list[str] = Field(default_factory=list)
    booking_date: date | None = None
    scheduled_departure: datetime
    scheduled_arrival: datetime | None = None
    actual_arrival: datetime | None = None
    disruption_type: DisruptionType
    cause_code: str
    departure_delay_minutes: int = 0
    arrival_delay_minutes: int | None = None
    connection_departure_delay_minutes: int | None = None
    notice_given_days: int | None = None
    brought_forward_minutes: int = 0
    rerouting_offered: ReroutingOffer | None = None
    single_booking: bool = True
    volunteer: bool = False
    fare_type: FareType = "public"
    check_in_ok: bool = True
    nights_needed: int = 0
    passenger_flags: list[VulnerabilityFlag] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive(self) -> DisruptionCase:
        if self.scheduled_departure.tzinfo is None:
            raise ValueError("scheduled_departure must be timezone-aware")
        if self.arrival_delay_minutes is None and self.actual_arrival and self.scheduled_arrival:
            minutes = int((self.actual_arrival - self.scheduled_arrival).total_seconds() // 60)
            object.__setattr__(self, "arrival_delay_minutes", minutes)
        if self.destination_iata is None:
            first_stop = self.connection_iatas[0] if self.connection_iatas else None
            object.__setattr__(self, "destination_iata", first_stop or self.final_destination_iata)
        return self


Tier = Literal["S1", "S2", "S3", "S4"]
Urgency = Literal["none", "soon", "immediate"]


class Persona(BaseModel):
    """Who is writing, and how (FR-13.1). Same case + different persona = counterfactual variant."""

    name: str
    temperament: Literal["calm", "anxious", "frustrated", "angry", "sarcastic"]
    english: Literal["native", "fluent", "basic"]
    honesty: Literal["honest", "exaggerating", "inconsistent"]
    loyalty_tier: Literal["none", "silver", "gold"]
    injection_attempt: bool = False
    style_note: str = ""


class Situation(BaseModel):
    """Where the passenger is and what is at stake when they write."""

    timing: Literal["past", "upcoming", "at_airport"]
    urgency: Urgency = "none"
    deadline_minutes: int | None = None
    deadline_reason: str | None = None
    frustration: int = Field(default=2, ge=1, le=5)
    repeat_contact_count: int = 0
    s4_trigger: Literal["medical_urgent", "minor_stranded", "safety_threat", "crisis"] | None = None
    notes: list[str] = Field(default_factory=list)


TIER_ORDER: dict[str, int] = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}
URGENCY_ORDER: dict[str, int] = {"none": 0, "soon": 1, "immediate": 2}


class TriageResult(BaseModel):
    """Severity of one passenger message (FR-3.1, SPEC §5)."""

    tier: Tier
    urgency: Urgency
    urgency_deadline: datetime | None = None
    urgency_deadline_minutes: int | None = None
    frustration: int = Field(ge=1, le=5)
    vulnerability_flags: list[VulnerabilityFlag] = Field(default_factory=list)
    repeat_contact_count: int = 0
    reasons: list[str] = Field(default_factory=list)
    source: Literal["rules", "llm", "merged"]
    rules_tier: Tier | None = None
    llm_tier: Tier | None = None
    llm_confidence: float | None = None


class PlantedError(BaseModel):
    """A known defect planted into a good reply (judge evaluation, Step 7b)."""

    error_type: str
    dimension: str
    description: str


class CareObligation(BaseModel):
    item: CareItem
    detail: str
    article: str


class RerouteRefundRights(BaseModel):
    refund_within_days: int
    options: list[str]
    detail: str


class RuleStep(BaseModel):
    rule: str
    result: str
    article: str | None = None


class ToolCallRecord(BaseModel):
    """One agent tool call, kept with each draft attempt (SPEC §5 DraftAttempt.tool_calls)."""

    tool: str
    args: dict[str, object]
    result: dict[str, object] | list[dict[str, object]] | None = None
    error: str | None = None
    idempotency_key: str | None = None
    at: datetime


class Entitlement(BaseModel):
    regime: Regime
    ruleset_version: str
    in_scope: bool
    compensation_eur: int | None
    compensation_reason: str
    may_reduce_to_eur: int | None = None
    reduction_applied: bool = False  # the engine never applies optional reductions itself
    distance_km: float | None = None
    band: str | None = None
    care: list[CareObligation] = Field(default_factory=list)
    hotel_nights_owed: int | None = None
    refund_or_rerouting: RerouteRefundRights | None = None
    voucher_allowed_only_with_consent: bool = True
    voucher_requires: str | None = None
    claim_deadline: str | None = None
    articles: list[str] = Field(default_factory=list)
    reason_trace: list[RuleStep] = Field(default_factory=list)
    unverified_rules: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    confidence: Literal["verified", "unverified"] = "verified"

    @property
    def care_items(self) -> set[str]:
        return {c.item for c in self.care}


class CheckResult(BaseModel):
    """One deterministic code check on a draft (FR-7)."""

    check_id: str
    passed: bool
    detail: str = ""


class DraftAttempt(BaseModel):
    attempt_id: str
    conversation_id: str
    attempt_no: int
    draft_text: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    code_checks: list[CheckResult] = Field(default_factory=list)
    model: str = ""
    created_at: datetime


class Resolution(BaseModel):
    decision: str
    notes: str = ""
    compensation_paid_eur: int | None = None
    resolved_at: datetime


class HandoffTicket(BaseModel):
    ticket_id: str
    conversation_id: str
    tier: Tier
    urgency: Urgency
    summary: str
    entitlement: Entitlement | None
    actions_tried: list[str] = Field(default_factory=list)
    passenger_state: str
    recommended_next_step: str
    reason: str
    status: Literal["open", "acknowledged", "resolved", "escalated"] = "open"
    sla_due_at: datetime
    resolution: Resolution | None = None


class Booking(BaseModel):
    """What the airline's booking system knows about a passenger's trip (mock)."""

    booking_ref: str
    passenger_ref: str
    passenger_name: str
    loyalty_tier: Literal["none", "silver", "gold"] = "none"
    fare_eur: int = 200
    case: DisruptionCase


class Scenario(BaseModel):
    """A synthetic evaluation case with ground truth (SPEC §5, §10.1)."""

    scenario_id: str
    template_id: str
    split: Literal["dev", "test"]
    passenger_ref: str
    booking_ref: str
    case: DisruptionCase
    persona: Persona
    situation: Situation
    opening_message: str = ""
    gold_entitlement: Entitlement
    gold_tier: Tier
    gold_key_actions: list[str]
    follow_ups: list[str] = Field(default_factory=list)
    counterfactual_group: str | None = None
    variant: str | None = None  # which attribute differs from the group's base scenario
    planted_error: PlantedError | None = None
