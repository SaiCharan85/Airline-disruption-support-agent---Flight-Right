from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from flightright.config import load_config
from flightright.services.actions import ActionStore
from flightright.services.airline import MockAirline
from flightright.services.history import HistoryStore, PriorCase, Resolution
from flightright.services.playbooks import Playbooks
from flightright.services.tools import AgentTools
from flightright.services.types import (
    AuthorityExceededError,
    FlightStatus,
    IdempotencyConflictError,
    RebookingOption,
    UnknownFlightError,
)

NOW = datetime(2027, 11, 15, 9, 0, tzinfo=UTC)


@pytest.fixture
def airline() -> MockAirline:
    return MockAirline(load_config().authority, ActionStore(), seed=42, clock=lambda: NOW)


@pytest.fixture
def tools(airline: MockAirline) -> AgentTools:
    return AgentTools(airline, HistoryStore(clock=lambda: NOW), Playbooks(), clock=lambda: NOW)


def _options(airline: MockAirline, **kw: object) -> list[RebookingOption]:
    args: dict[str, object] = {
        "flight_no": "LH1166",
        "date": "2027-11-15",
        "carrier_iata": "LH",
        "origin_iata": "FRA",
        "destination_iata": "LIS",
        "distance_km": 1874,
        "intra_eu": True,
    } | kw
    return airline.get_rebooking_options(**args)  # type: ignore[arg-type]


# ------------------------------------------------------------------ rebooking and flight status


def test_FR_5_2_rebooking_is_deterministic_and_sorted(airline: MockAirline) -> None:
    first, second = _options(airline), _options(airline)
    assert first == second and len(first) >= 3
    arrivals = [o.arrival_shift_minutes for o in first]
    assert arrivals == sorted(arrivals)
    assert {"same_carrier", "same_carrier_next_day", "other_carrier"} <= {o.kind for o in first}


def test_rail_offered_only_for_short_intra_eu_journeys(airline: MockAirline) -> None:
    short = _options(airline, origin_iata="MAD", destination_iata="BCN", distance_km=483)
    long_haul = _options(
        airline, origin_iata="CDG", destination_iata="JFK", distance_km=5834, intra_eu=False
    )
    assert any(o.kind == "rail" for o in short)
    assert not any(o.kind == "rail" for o in long_haul)
    assert any(o.kind == "alternative_airport" and o.to_iata == "EWR" for o in long_haul)


def test_pinned_rebooking_options_win(airline: MockAirline) -> None:
    pinned = [
        RebookingOption(
            option_id="X",
            kind="rail",
            carrier_iata="RAIL",
            flight_no="R1",
            from_iata="FRA",
            to_iata="LIS",
            departure_shift_minutes=0,
            arrival_shift_minutes=10,
            seats_available=True,
        )
    ]
    airline.register_rebooking("LH1166", "2027-11-15", pinned)
    assert _options(airline) == pinned


def test_flight_status_registry(airline: MockAirline) -> None:
    status = FlightStatus(
        flight_no="LH1166",
        date="2027-11-15",
        origin_iata="FRA",
        destination_iata="LIS",
        status="delayed",
        scheduled_departure=NOW,
        estimated_departure=NOW + timedelta(hours=3),
    )
    airline.register_flight(status)
    assert airline.get_flight_status("LH1166", "2027-11-15") == status
    with pytest.raises(UnknownFlightError):
        airline.get_flight_status("LH0000", "2027-11-15")


# ------------------------------------------------------------ authority limits and idempotency


def test_NFR_4_duplicate_voucher_request_issues_once(airline: MockAirline) -> None:
    first = airline.issue_care_voucher("c1", "meal", 25, "msg-1:meal")
    again = airline.issue_care_voucher("c1", "meal", 25, "msg-1:meal")
    assert first == again and airline.actions.count("care_voucher") == 1
    assert airline.actions.total_eur("care_voucher", "c1") == 25


def test_idempotency_key_reused_with_other_params_is_refused(airline: MockAirline) -> None:
    airline.issue_care_voucher("c1", "meal", 25, "k")
    with pytest.raises(IdempotencyConflictError):
        airline.issue_care_voucher("c1", "meal", 30, "k")


def test_FR_5_3_voucher_above_per_item_cap_needs_a_human(airline: MockAirline) -> None:
    with pytest.raises(AuthorityExceededError, match="meal"):
        airline.issue_care_voucher("c1", "meal", 31, "k")


def test_FR_5_3_voucher_total_cap_per_conversation(airline: MockAirline) -> None:
    airline.issue_care_voucher("c1", "hotel_night", 180, "h", quantity=3)  # 540 of 600
    with pytest.raises(AuthorityExceededError, match="total"):
        airline.issue_care_voucher("c1", "meal", 30, "m1", quantity=3)
    assert airline.actions.total_eur("care_voucher", "c1") == 540
    airline.issue_care_voucher("c2", "meal", 30, "m2")  # other conversations are separate


@pytest.mark.parametrize(("amount", "quantity"), [(0, 1), (10, 0)])
def test_voucher_rejects_non_positive_values(
    airline: MockAirline, amount: int, quantity: int
) -> None:
    with pytest.raises(ValueError, match="positive"):
        airline.issue_care_voucher("c1", "meal", amount, "k", quantity)


def test_refund_idempotent_and_capped(airline: MockAirline) -> None:
    first = airline.initiate_refund("ABC123", 300, "cancellation", "r1")
    assert first == airline.initiate_refund("ABC123", 300, "cancellation", "r1")
    assert first.due_within_days == 7 and airline.actions.count("refund") == 1
    with pytest.raises(AuthorityExceededError):
        airline.initiate_refund("ABC123", 900, "cancellation", "r2")
    with pytest.raises(ValueError, match="positive"):
        airline.initiate_refund("ABC123", 0, "x", "r3")


def test_ticket_idempotent(airline: MockAirline) -> None:
    t1 = airline.create_ticket("c1", "S3", "Missed connection, elderly passenger", "t1")
    assert t1 == airline.create_ticket("c1", "S3", "Missed connection, elderly passenger", "t1")
    assert t1.ticket_id.startswith("T") and airline.actions.count("ticket") == 1


def test_action_store_persists(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite3"
    a = MockAirline(load_config().authority, ActionStore(path), clock=lambda: NOW)
    a.issue_care_voucher("c1", "meal", 20, "k")
    b = MockAirline(load_config().authority, ActionStore(path), clock=lambda: NOW)
    assert (
        b.issue_care_voucher("c1", "meal", 20, "k").voucher_id
        == a.issue_care_voucher("c1", "meal", 20, "k").voucher_id
    )
    assert b.actions.count("care_voucher") == 1


# ------------------------------------------------------------------ history


def test_FR_10_2_history_cases_contacts_resolutions(tmp_path: Path) -> None:
    clock = {"now": NOW - timedelta(days=3)}
    store = HistoryStore(tmp_path / "h.sqlite3", clock=lambda: clock["now"])
    store.add_case(
        "P1",
        PriorCase(
            case_id="K1",
            booking_ref="ABC123",
            disruption_type="delay",
            summary="3h delay FRA-LIS",
            status="open",
            opened_at=clock["now"],
        ),
    )
    store.record_contact("P1", "conv-1", "ABC123", "telegram")
    store.record_contact("P1", "conv-1", "ABC123", "telegram")  # duplicate delivery: one contact
    clock["now"] = NOW
    store.record_contact("P1", "conv-2", "ABC123", "email")
    store.add_resolution(
        Resolution(
            case_id="K1",
            decision="compensation approved",
            notes="paid",
            compensation_paid_eur=400,
            resolved_at=NOW,
        )
    )
    store.set_preference("P1", "language", "pt")
    h = HistoryStore(tmp_path / "h.sqlite3").get("P1")
    assert [c.status for c in h.cases] == ["resolved"]
    assert len(h.contacts) == 2 and h.repeat_contacts("ABC123", NOW) == 2
    assert h.repeat_contacts("ABC123", NOW + timedelta(days=30)) == 0
    assert h.resolutions[0].compensation_paid_eur == 400 and h.preferences == {"language": "pt"}


def test_unknown_passenger_has_empty_history() -> None:
    h = HistoryStore().get("nobody")
    assert h.cases == [] and h.contacts == [] and h.repeat_contacts("X", NOW) == 0


# ------------------------------------------------------------------ playbooks and tool records


@pytest.mark.parametrize("dtype", ["delay", "cancellation", "denied_boarding", "missed_connection"])
def test_FR_10_4_playbook_per_disruption_type(dtype: str) -> None:
    pb = Playbooks().get(dtype)
    assert pb.text.startswith("# Playbook") and len(pb.version) == 12
    assert "voucher" in pb.text.lower()  # every playbook repeats the cash-right rule (FR-6.3)


def test_unknown_playbook_raises() -> None:
    with pytest.raises(KeyError):
        Playbooks().get("baggage")


def test_tools_record_successes_and_failures(tools: AgentTools) -> None:
    tools.get_playbook("delay")
    tools.get_passenger_history("P1")
    tools.get_rebooking_options("LH1166", "2027-11-15", "LH", "FRA", "LIS", 1874, True)
    tools.issue_care_voucher("c1", "meal", 25, "k1")
    tools.initiate_refund("ABC123", 200, "cancellation", "r1")
    tools.create_ticket("c1", "S2", "summary", "t1")
    with pytest.raises(AuthorityExceededError):
        tools.issue_care_voucher("c1", "meal", 99, "k2")
    names = [c.tool for c in tools.calls]
    assert names == [
        "get_playbook",
        "get_passenger_history",
        "get_rebooking_options",
        "issue_care_voucher",
        "initiate_refund",
        "create_ticket",
        "issue_care_voucher",
    ]
    assert tools.calls[3].idempotency_key == "k1" and tools.calls[3].result
    assert tools.calls[-1].error and "AuthorityExceededError" in tools.calls[-1].error
    assert isinstance(tools.calls[2].result, list) and tools.calls[2].result
