"""Records returned by the mock airline services (FR-5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

CareVoucherType = Literal[
    "refreshments", "meal", "hotel_night", "hotel_transport", "communications"
]
RebookingKind = Literal[
    "same_carrier", "same_carrier_next_day", "other_carrier", "alternative_airport", "rail"
]


class ServiceError(Exception):
    """Base for mock-service failures the agent must handle."""


class AuthorityExceededError(ServiceError):
    """The action is above what the agent may do without a human (FR-5.3) -> handoff."""


class IdempotencyConflictError(ServiceError):
    """The same idempotency key was reused with different parameters."""


class UnknownFlightError(ServiceError, KeyError):
    pass


class FlightStatus(BaseModel):
    flight_no: str
    date: str
    origin_iata: str
    destination_iata: str
    status: Literal["scheduled", "delayed", "cancelled", "departed", "arrived", "diverted"]
    scheduled_departure: datetime
    estimated_departure: datetime | None = None
    cause_code: str | None = None
    note: str = ""


class RebookingOption(BaseModel):
    option_id: str
    kind: RebookingKind
    carrier_iata: str
    flight_no: str
    from_iata: str
    to_iata: str
    departure_shift_minutes: int  # versus the original scheduled departure
    arrival_shift_minutes: int  # versus the original scheduled arrival
    seats_available: bool
    note: str = ""


class CareVoucher(BaseModel):
    voucher_id: str
    conversation_id: str
    type: CareVoucherType
    amount_eur: int
    quantity: int = 1
    issued_at: datetime


class RefundRecord(BaseModel):
    refund_id: str
    booking_ref: str
    amount_eur: int
    reason: str
    status: Literal["initiated"] = "initiated"
    due_within_days: int
    initiated_at: datetime


class TicketRecord(BaseModel):
    ticket_id: str
    conversation_id: str
    tier: str
    summary: str
    status: Literal["open"] = "open"
    created_at: datetime
