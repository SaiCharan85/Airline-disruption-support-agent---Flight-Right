"""Property-based tests (SPEC §9.2)."""

from datetime import date, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from flightright.models import DisruptionCase, ReroutingOffer
from flightright.rules.engine import EntitlementEngine

ROUTES = [
    ("MAD", "BCN"),
    ("FRA", "LIS"),
    ("AMS", "IST"),
    ("MUC", "DXB"),
    ("CDG", "JFK"),
    ("ATH", "FRA"),
]
EXTRAORDINARY = [
    "weather_unsafe",
    "atc_restriction",
    "bird_strike",
    "security_risk",
    "airport_closure",
]
DAYS = st.dates(min_value=date(2027, 1, 1), max_value=date(2028, 12, 31))


def _case(day: date, **kw: Any) -> DisruptionCase:
    base: dict[str, Any] = {
        "carrier_iata": "XX",
        "carrier_is_eu": True,
        "origin_iata": "FRA",
        "final_destination_iata": "LIS",
        "scheduled_departure": f"{day.isoformat()}T09:00:00+01:00",
        "disruption_type": "delay",
        "cause_code": "technical_fault",
        "booking_date": day.isoformat(),
    }
    base.update(kw)
    return DisruptionCase.model_validate(base)


@settings(max_examples=150, deadline=None)
@given(day=DAYS, late=st.integers(0, 1200), dep=st.integers(0, 1200), route=st.sampled_from(ROUTES))
def test_compensation_never_negative(
    engine: EntitlementEngine, day: date, late: int, dep: int, route: tuple[str, str]
) -> None:
    got = engine.compute_entitlement(
        _case(
            day,
            origin_iata=route[0],
            final_destination_iata=route[1],
            arrival_delay_minutes=late,
            departure_delay_minutes=dep,
        )
    )
    assert got.compensation_eur is None or got.compensation_eur > 0
    assert got.may_reduce_to_eur is None or 0 < got.may_reduce_to_eur <= (got.compensation_eur or 0)


@settings(max_examples=60, deadline=None)
@given(day=DAYS, late=st.integers(181, 1200))
def test_compensation_monotonic_in_distance_band(
    engine: EntitlementEngine, day: date, late: int
) -> None:
    amounts = []
    for origin, dest in [
        ("MAD", "BCN"),
        ("AMS", "IST"),
        ("MUC", "DXB"),
    ]:  # bands a, b, c (non intra-EU b/c)
        got = engine.compute_entitlement(
            _case(day, origin_iata=origin, final_destination_iata=dest, arrival_delay_minutes=late)
        )
        amounts.append(got.compensation_eur or 0)
    assert amounts == sorted(amounts)


@settings(max_examples=150, deadline=None)
@given(
    day=DAYS,
    cause=st.sampled_from(EXTRAORDINARY),
    dtype=st.sampled_from(["delay", "cancellation"]),
    dep=st.integers(0, 900),
    nights=st.integers(0, 6),
)
def test_extraordinary_circumstances_never_remove_care(
    engine: EntitlementEngine, day: date, cause: str, dtype: str, dep: int, nights: int
) -> None:
    kw: dict[str, Any] = {
        "disruption_type": dtype,
        "departure_delay_minutes": dep,
        "arrival_delay_minutes": dep,
        "nights_needed": nights,
    }
    if dtype == "cancellation":
        kw["notice_given_days"] = 0
    ordinary = engine.compute_entitlement(_case(day, cause_code="technical_fault", **kw))
    extraordinary = engine.compute_entitlement(_case(day, cause_code=cause, **kw))
    assert extraordinary.compensation_eur is None
    assert {c.item for c in extraordinary.care} - {"hotel", "hotel_transport"} == {
        c.item for c in ordinary.care
    } - {"hotel", "hotel_transport"}
    assert (extraordinary.hotel_nights_owed is None) == (ordinary.hotel_nights_owed is None)


@settings(max_examples=150, deadline=None)
@given(
    day=DAYS,
    notice=st.integers(0, 30),
    extra=st.integers(1, 30),
    early=st.integers(0, 240),
    late=st.integers(0, 600),
)
def test_longer_notice_never_increases_compensation(
    engine: EntitlementEngine, day: date, notice: int, extra: int, early: int, late: int
) -> None:
    offer = ReroutingOffer(departure_early_minutes=early, arrival_late_minutes=late)
    short = engine.compute_entitlement(
        _case(
            day, disruption_type="cancellation", notice_given_days=notice, rerouting_offered=offer
        )
    )
    longer = engine.compute_entitlement(
        _case(
            day,
            disruption_type="cancellation",
            notice_given_days=notice + extra,
            rerouting_offered=offer,
        )
    )
    assert (longer.compensation_eur or 0) <= (short.compensation_eur or 0)


@settings(max_examples=100, deadline=None)
@given(offset=st.integers(-400, 400))
def test_regime_flips_exactly_at_boundary(engine: EntitlementEngine, offset: int) -> None:
    day = engine.boundary + timedelta(days=offset)
    expected = "eu261_amended_2027" if offset >= 0 else "eu261_2004"
    assert engine.select_regime(_case(day)) == expected
