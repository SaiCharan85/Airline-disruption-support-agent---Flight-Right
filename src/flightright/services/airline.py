"""Mock airline back office (FR-5): flight status, rebooking, care vouchers, refunds, tickets.

Behaviour is deterministic for a given seed and flight, so scenarios and tests are reproducible.
Real airline systems are out of scope; these mocks sit behind the same interface a real
integration would use. Side effects go through ActionStore (idempotent) and the authority limits
in config (anything above them raises AuthorityExceededError -> handoff).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from datetime import UTC, datetime

from flightright.config import AuthorityConfig
from flightright.services.actions import ActionStore
from flightright.services.types import (
    AuthorityExceededError,
    CareVoucher,
    CareVoucherType,
    FlightStatus,
    RebookingOption,
    RefundRecord,
    TicketRecord,
    UnknownFlightError,
)

# Mock data: secondary airports serving the same city (used for "alternative airport" rerouting).
ALTERNATIVE_AIRPORTS = {
    "LHR": "LGW",
    "LGW": "LHR",
    "CDG": "ORY",
    "ORY": "CDG",
    "FCO": "CIA",
    "CIA": "FCO",
    "MXP": "LIN",
    "LIN": "MXP",
    "JFK": "EWR",
    "EWR": "JFK",
    "IST": "SAW",
    "SAW": "IST",
}
RAIL_MAX_KM = 700  # rerouting by rail is offered for short intra-EU journeys
REFUND_WITHIN_DAYS = 7


def _short_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10].upper()


class MockAirline:
    def __init__(
        self,
        authority: AuthorityConfig,
        actions: ActionStore,
        seed: int = 42,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.authority = authority
        self.actions = actions
        self.seed = seed
        self.clock = clock
        self._flights: dict[tuple[str, str], FlightStatus] = {}
        self._rebooking: dict[tuple[str, str], list[RebookingOption]] = {}

    # ------------------------------------------------------------------ flights

    def register_flight(self, status: FlightStatus) -> None:
        """Scenarios register the disrupted flight; the context loader reads it back."""
        self._flights[(status.flight_no, status.date)] = status

    def register_rebooking(self, flight_no: str, date: str, options: list[RebookingOption]) -> None:
        """Pin specific options for a scenario instead of the generated ones."""
        self._rebooking[(flight_no, date)] = options

    def get_flight_status(self, flight_no: str, date: str) -> FlightStatus:
        try:
            return self._flights[(flight_no, date)]
        except KeyError:
            raise UnknownFlightError(f"{flight_no} on {date}") from None

    def get_rebooking_options(
        self,
        flight_no: str,
        date: str,
        carrier_iata: str,
        origin_iata: str,
        destination_iata: str,
        distance_km: float,
        intra_eu: bool,
    ) -> list[RebookingOption]:
        pinned = self._rebooking.get((flight_no, date))
        if pinned is not None:
            return pinned
        rng = random.Random(f"{self.seed}:{flight_no}:{date}:{origin_iata}:{destination_iata}")
        options: list[RebookingOption] = []

        def add(
            kind: str,
            carrier: str,
            dep: int,
            arr: int,
            to: str = destination_iata,
            frm: str = origin_iata,
            note: str = "",
        ) -> None:
            number = (
                f"{carrier}{rng.randint(100, 9899)}"
                if kind != "rail"
                else f"RAIL-{rng.randint(10, 99)}"
            )
            options.append(
                RebookingOption(
                    option_id=_short_id("R", flight_no, date, kind, str(len(options))),
                    kind=kind,  # type: ignore[arg-type]
                    carrier_iata=carrier,
                    flight_no=number,
                    from_iata=frm,
                    to_iata=to,
                    departure_shift_minutes=dep,
                    arrival_shift_minutes=arr,
                    seats_available=rng.random() < 0.8,
                    note=note,
                )
            )

        same = rng.randint(90, 300)
        add("same_carrier", carrier_iata, same, same + rng.randint(-15, 30))
        next_day = rng.randint(900, 1200)
        add(
            "same_carrier_next_day",
            carrier_iata,
            next_day,
            next_day + rng.randint(-15, 30),
            note="overnight stay needed",
        )
        other = rng.randint(120, 360)
        add(
            "other_carrier",
            rng.choice(["XQ", "ZB", "YP"]),
            other,
            other + rng.randint(0, 60),
            note="operated by another airline",
        )
        alt = ALTERNATIVE_AIRPORTS.get(destination_iata)
        if alt:
            shift = rng.randint(60, 240)
            add(
                "alternative_airport",
                carrier_iata,
                shift,
                shift + rng.randint(0, 45),
                to=alt,
                note=f"lands at {alt}; transfer to {destination_iata} paid by the airline",
            )
        if intra_eu and distance_km <= RAIL_MAX_KM:
            shift = rng.randint(30, 120)
            add(
                "rail",
                "RAIL",
                shift,
                shift + rng.randint(60, 240),
                note="train, another mode of transport",
            )
        return sorted(options, key=lambda o: o.arrival_shift_minutes)

    # ------------------------------------------------------------------ side effects

    def issue_care_voucher(
        self,
        conversation_id: str,
        voucher_type: CareVoucherType,
        amount_eur: int,
        idempotency_key: str,
        quantity: int = 1,
    ) -> CareVoucher:
        if amount_eur <= 0 or quantity <= 0:
            raise ValueError("voucher amount and quantity must be positive")
        cap = self.authority.care_voucher_max_eur[voucher_type]
        if amount_eur > cap:
            raise AuthorityExceededError(
                f"{voucher_type} voucher of EUR {amount_eur} exceeds EUR {cap}"
            )
        total = amount_eur * quantity
        params: dict[str, object] = {
            "conversation_id": conversation_id,
            "type": voucher_type,
            "amount_eur": amount_eur,
            "quantity": quantity,
        }

        def create() -> CareVoucher:
            already = self.actions.total_eur("care_voucher", conversation_id)
            limit = self.authority.care_vouchers_max_total_eur
            if already + total > limit:
                raise AuthorityExceededError(
                    f"care vouchers would total EUR {already + total} (limit EUR {limit})"
                )
            return CareVoucher(
                voucher_id=_short_id("V", idempotency_key),
                conversation_id=conversation_id,
                type=voucher_type,
                amount_eur=amount_eur,
                quantity=quantity,
                issued_at=self.clock(),
            )

        voucher, _ = self.actions.once(
            "care_voucher",
            idempotency_key,
            params,
            CareVoucher,
            create,
            conversation_id=conversation_id,
            amount_eur=total,
        )
        return voucher

    def initiate_refund(
        self, booking_ref: str, amount_eur: int, reason: str, idempotency_key: str
    ) -> RefundRecord:
        if amount_eur <= 0:
            raise ValueError("refund amount must be positive")
        limit = self.authority.refund_auto_max_eur
        if amount_eur > limit:
            raise AuthorityExceededError(f"refund of EUR {amount_eur} exceeds EUR {limit}")
        params: dict[str, object] = {
            "booking_ref": booking_ref,
            "amount_eur": amount_eur,
            "reason": reason,
        }

        def create() -> RefundRecord:
            return RefundRecord(
                refund_id=_short_id("F", idempotency_key),
                booking_ref=booking_ref,
                amount_eur=amount_eur,
                reason=reason,
                due_within_days=REFUND_WITHIN_DAYS,
                initiated_at=self.clock(),
            )

        refund, _ = self.actions.once(
            "refund", idempotency_key, params, RefundRecord, create, amount_eur=amount_eur
        )
        return refund

    def create_ticket(
        self, conversation_id: str, tier: str, summary: str, idempotency_key: str
    ) -> TicketRecord:
        params: dict[str, object] = {
            "conversation_id": conversation_id,
            "tier": tier,
            "summary": summary,
        }

        def create() -> TicketRecord:
            return TicketRecord(
                ticket_id=_short_id("T", idempotency_key),
                conversation_id=conversation_id,
                tier=tier,
                summary=summary,
                created_at=self.clock(),
            )

        ticket, _ = self.actions.once(
            "ticket", idempotency_key, params, TicketRecord, create, conversation_id=conversation_id
        )
        return ticket
