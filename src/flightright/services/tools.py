"""The agent's tools (FR-5.2), each call recorded as a ToolCallRecord for drafts and audits.

Failures (authority exceeded, idempotency conflict, unknown flight) are recorded and re-raised so
the graph can route them (usually to handoff).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel

from flightright.models import ToolCallRecord
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore, PassengerHistory
from flightright.services.playbooks import Playbook, Playbooks
from flightright.services.types import (
    CareVoucher,
    CareVoucherType,
    RebookingOption,
    RefundRecord,
    TicketRecord,
)

T = TypeVar("T")


def _dump(value: object) -> dict[str, object] | list[dict[str, object]] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [v.model_dump(mode="json") for v in value if isinstance(v, BaseModel)]
    return None


class AgentTools:
    def __init__(
        self,
        airline: MockAirline,
        history: HistoryStore,
        playbooks: Playbooks,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.airline = airline
        self.history = history
        self.playbooks = playbooks
        self.clock = clock
        self.calls: list[ToolCallRecord] = []

    def _record(
        self,
        tool: str,
        args: dict[str, object],
        fn: Callable[[], T],
        idempotency_key: str | None = None,
    ) -> T:
        try:
            value = fn()
        except Exception as exc:
            self.calls.append(
                ToolCallRecord(
                    tool=tool,
                    args=args,
                    error=f"{type(exc).__name__}: {exc}",
                    idempotency_key=idempotency_key,
                    at=self.clock(),
                )
            )
            raise
        self.calls.append(
            ToolCallRecord(
                tool=tool,
                args=args,
                result=_dump(value),
                idempotency_key=idempotency_key,
                at=self.clock(),
            )
        )
        return value

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
        args: dict[str, object] = {
            "flight_no": flight_no,
            "date": date,
            "origin": origin_iata,
            "destination": destination_iata,
        }
        return self._record(
            "get_rebooking_options",
            args,
            lambda: self.airline.get_rebooking_options(
                flight_no, date, carrier_iata, origin_iata, destination_iata, distance_km, intra_eu
            ),
        )

    def issue_care_voucher(
        self,
        conversation_id: str,
        voucher_type: CareVoucherType,
        amount_eur: int,
        idempotency_key: str,
        quantity: int = 1,
    ) -> CareVoucher:
        args: dict[str, object] = {
            "type": voucher_type,
            "amount_eur": amount_eur,
            "quantity": quantity,
        }
        return self._record(
            "issue_care_voucher",
            args,
            lambda: self.airline.issue_care_voucher(
                conversation_id, voucher_type, amount_eur, idempotency_key, quantity
            ),
            idempotency_key,
        )

    def initiate_refund(
        self, booking_ref: str, amount_eur: int, reason: str, idempotency_key: str
    ) -> RefundRecord:
        args: dict[str, object] = {
            "booking_ref": booking_ref,
            "amount_eur": amount_eur,
            "reason": reason,
        }
        return self._record(
            "initiate_refund",
            args,
            lambda: self.airline.initiate_refund(booking_ref, amount_eur, reason, idempotency_key),
            idempotency_key,
        )

    def create_ticket(
        self, conversation_id: str, tier: str, summary: str, idempotency_key: str
    ) -> TicketRecord:
        args: dict[str, object] = {"tier": tier, "summary": summary}
        return self._record(
            "create_ticket",
            args,
            lambda: self.airline.create_ticket(conversation_id, tier, summary, idempotency_key),
            idempotency_key,
        )

    def get_playbook(self, disruption_type: str) -> Playbook:
        return self._record(
            "get_playbook",
            {"disruption_type": disruption_type},
            lambda: self.playbooks.get(disruption_type),
        )

    def get_passenger_history(self, passenger_ref: str) -> PassengerHistory:
        return self._record(
            "get_passenger_history",
            {"passenger_ref": passenger_ref},
            lambda: self.history.get(passenger_ref),
        )
