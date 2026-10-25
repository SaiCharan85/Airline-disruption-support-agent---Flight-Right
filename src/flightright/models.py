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
