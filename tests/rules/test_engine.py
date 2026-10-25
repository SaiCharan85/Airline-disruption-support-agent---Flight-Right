"""Table-driven tests per rule branch (FR-4), named after the article where one applies."""

from datetime import date
from typing import Any

import pytest

from flightright.models import DisruptionCase, ReroutingOffer
from flightright.rules.engine import EntitlementEngine, compare

OLD_DAY = "2027-03-15T09:00:00+01:00"
NEW_DAY = "2027-11-15T09:00:00+01:00"


def make(**overrides: Any) -> DisruptionCase:
    base: dict[str, Any] = {
        "carrier_iata": "LH",
        "carrier_is_eu": True,
        "origin_iata": "FRA",
        "final_destination_iata": "LIS",
        "scheduled_departure": OLD_DAY,
        "disruption_type": "delay",
        "cause_code": "technical_fault",
        "booking_date": "2027-01-10",
    }
    base.update(overrides)
    return DisruptionCase.model_validate(base)


# ------------------------------------------------------------------ helpers and models


@pytest.mark.parametrize(
    ("value", "threshold", "op", "expected"),
    [
        (180, 180, "gte", True),
        (180, 180, "gt", False),
        (240, 240, "lte", True),
        (120, 120, "lt", False),
    ],
)
def test_compare_boundaries(value: int, threshold: int, op: str, expected: bool) -> None:
    assert compare(value, threshold, op) is expected


def test_compare_rejects_unknown_operator() -> None:
    with pytest.raises(ValueError, match="unknown comparison"):
        compare(1, 1, "eq")


def test_case_requires_timezone_aware_departure() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make(scheduled_departure="2027-03-15T09:00:00")


def test_arrival_delay_is_derived_from_datetimes() -> None:
    case = make(
        scheduled_arrival="2027-03-15T11:00:00+00:00", actual_arrival="2027-03-15T14:05:00+00:00"
    )
    assert case.arrival_delay_minutes == 185


def test_first_leg_destination_defaults_to_first_connection() -> None:
    assert make(connection_iatas=["BRU"], final_destination_iata="HAM").destination_iata == "BRU"


# ------------------------------------------------------------------ regime and scope


def test_FR_4_2_regime_flips_exactly_at_boundary(engine: EntitlementEngine) -> None:
    assert (
        engine.select_regime(make(scheduled_departure="2027-10-22T23:30:00+02:00")) == "eu261_2004"
    )
    assert (
        engine.select_regime(make(scheduled_departure="2027-10-23T00:10:00+02:00"))
        == "eu261_amended_2027"
    )
    assert engine.boundary == date(2027, 10, 23)


def test_art_3_2_a_no_check_in_means_out_of_scope(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(make(check_in_ok=False, arrival_delay_minutes=300))
    assert not got.in_scope and "3(2)(a)" in got.articles


def test_art_3_2_a_check_in_not_required_for_cancellation(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(disruption_type="cancellation", check_in_ok=False, notice_given_days=1)
    )
    assert got.in_scope


def test_eea_airport_is_covered_but_unverified(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(origin_iata="OSL", final_destination_iata="FRA", arrival_delay_minutes=200)
    )
    assert got.in_scope and got.compensation_eur == 250  # ~1,100 km
    assert (
        got.confidence == "unverified"
        and "scope.territory.eea_and_switzerland" in got.unverified_rules
    )


def test_outermost_region_counts_as_eu(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            origin_iata="RUN",
            final_destination_iata="CDG",
            carrier_iata="UU",
            carrier_is_eu=True,
            arrival_delay_minutes=300,
            departure_delay_minutes=300,
        )
    )
    # Réunion -> Paris is intra-EU: > 1500 km pays EUR 400 whatever the distance (Art. 7(1)(b)).
    assert got.in_scope and got.band == "b" and got.compensation_eur == 400


def test_unknown_airport_raises(engine: EntitlementEngine) -> None:
    with pytest.raises(KeyError):
        engine.compute_entitlement(make(origin_iata="XXX"))


# ------------------------------------------------------------------ distance and bands


def test_FR_4_4_near_1500_km_edge_needs_review(engine: EntitlementEngine) -> None:
    # Dublin -> Madrid is ~1450 km: outside a 10 km tolerance, so verified.
    got = engine.compute_entitlement(
        make(origin_iata="DUB", final_destination_iata="MAD", arrival_delay_minutes=200)
    )
    assert got.band == "a" and got.confidence == "verified"


def test_near_band_edge_flags(engine: EntitlementEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    rules = engine.rulesets["eu261_2004"]
    monkeypatch.setitem(rules["distance"], "boundary_tolerance_km", 100)
    got = engine.compute_entitlement(
        make(origin_iata="DUB", final_destination_iata="MAD", arrival_delay_minutes=200)
    )
    assert got.review_reasons and got.confidence == "unverified"


def test_near_3500_km_non_eu_touches_unverified_boundary(
    engine: EntitlementEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = engine.rulesets["eu261_2004"]
    monkeypatch.setitem(rules["distance"], "boundary_tolerance_km", 1500)  # FRA-DXB is ~4,830 km
    got = engine.compute_entitlement(
        make(origin_iata="FRA", final_destination_iata="DXB", arrival_delay_minutes=200)
    )
    assert "compensation.boundary_3500_km" in got.unverified_rules


# ------------------------------------------------------------------ delays and connections


def test_art_6_1_iii_refund_option_from_five_hours(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(make(departure_delay_minutes=300, arrival_delay_minutes=300))
    assert got.refund_or_rerouting is not None and "6(1)(iii)" in got.articles


def test_amended_6_3_refund_option(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            departure_delay_minutes=310,
            arrival_delay_minutes=300,
        )
    )
    assert got.refund_or_rerouting is not None and "6(3)" in got.articles


def test_unknown_cause_is_never_assumed_extraordinary(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(make(cause_code="something_new", arrival_delay_minutes=250))
    assert got.compensation_eur is None and got.confidence == "unverified"
    assert any("unknown" in r for r in got.review_reasons)


def test_missed_connection_separate_tickets_2004_needs_review(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            disruption_type="missed_connection",
            single_booking=False,
            origin_iata="FCO",
            connection_iatas=["BRU"],
            final_destination_iata="HAM",
            arrival_delay_minutes=300,
        )
    )
    assert got.compensation_eur is None and got.confidence == "unverified"


def test_art_14_3_missed_connection_separate_tickets_amended(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="missed_connection",
            single_booking=False,
            origin_iata="FCO",
            connection_iatas=["BRU"],
            final_destination_iata="HAM",
            arrival_delay_minutes=300,
            connection_departure_delay_minutes=400,
        )
    )
    assert (
        got.compensation_eur is None and got.refund_or_rerouting is None and "14(3)" in got.articles
    )


@pytest.mark.parametrize("regime_day", [OLD_DAY, NEW_DAY])
def test_missed_connection_below_threshold(engine: EntitlementEngine, regime_day: str) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=regime_day,
            booking_date="2027-01-10" if regime_day == OLD_DAY else "2027-10-30",
            disruption_type="missed_connection",
            origin_iata="FCO",
            connection_iatas=["BRU"],
            final_destination_iata="HAM",
            arrival_delay_minutes=120,
        )
    )
    assert got.compensation_eur is None


def test_amended_6b_2_missed_connection_refund(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="missed_connection",
            origin_iata="FCO",
            connection_iatas=["BRU"],
            final_destination_iata="HAM",
            arrival_delay_minutes=400,
            connection_departure_delay_minutes=360,
        )
    )
    assert got.refund_or_rerouting is not None and "6b(2)" in got.articles


def test_amended_long_haul_delay_beyond_four_hours_has_no_reduction(
    engine: EntitlementEngine,
) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            origin_iata="CDG",
            final_destination_iata="JFK",
            carrier_iata="AF",
            arrival_delay_minutes=300,
        )
    )
    assert got.compensation_eur == 600 and got.may_reduce_to_eur is None


# ------------------------------------------------------------------ cancellations


def test_art_5_1_c_ii_exactly_seven_days_is_unverified(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            disruption_type="cancellation",
            notice_given_days=7,
            rerouting_offered=ReroutingOffer(departure_early_minutes=30, arrival_late_minutes=60),
        )
    )
    assert (
        got.compensation_eur is None
        and "cancellation.notice_exactly_7_days" in got.unverified_rules
    )


def test_art_5_1_c_no_rerouting_offered_means_compensation(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(make(disruption_type="cancellation", notice_given_days=10))
    assert got.compensation_eur == 400 and got.may_reduce_to_eur is None


def test_azurair_brought_forward_delay_becomes_cancellation_2004(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(disruption_type="delay", brought_forward_minutes=90, notice_given_days=2)
    )
    assert got.compensation_eur == 400 and "C-146/20 Azurair" in got.articles
    assert "cancellation.brought_forward_minutes_counts_as_cancellation" in got.unverified_rules


def test_amended_earlier_departure_reduction_25_percent_tier(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="cancellation",
            notice_given_days=3,
            rerouting_offered=ReroutingOffer(
                departure_early_minutes=150, arrival_late_minutes=-150, taken=True
            ),
        )
    )
    assert (
        got.compensation_eur == 400 and got.may_reduce_to_eur == 300 and "7(3)(b)" in got.articles
    )


def test_amended_earlier_departure_not_taken_no_reduction(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="cancellation",
            notice_given_days=3,
            rerouting_offered=ReroutingOffer(
                departure_early_minutes=150, arrival_late_minutes=-150
            ),
        )
    )
    assert got.may_reduce_to_eur is None


def test_amended_earlier_departure_beyond_three_hours_no_reduction(
    engine: EntitlementEngine,
) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="cancellation",
            notice_given_days=3,
            rerouting_offered=ReroutingOffer(
                departure_early_minutes=200, arrival_late_minutes=-200, taken=True
            ),
        )
    )
    assert got.may_reduce_to_eur is None


def test_cancellation_extraordinary_with_unknown_notice(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(disruption_type="cancellation", cause_code="atc_restriction")
    )
    assert got.compensation_eur is None and got.care_items == {
        "meals_refreshments",
        "communications",
    }


# ------------------------------------------------------------------ denied boarding


@pytest.mark.parametrize(("day", "article"), [(OLD_DAY, "4(1)"), (NEW_DAY, "4(2)")])
def test_art_4_volunteer_gets_no_compensation_or_care(
    engine: EntitlementEngine, day: str, article: str
) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=day,
            booking_date="2027-01-10" if day == OLD_DAY else "2027-10-30",
            disruption_type="denied_boarding",
            cause_code="overbooking",
            volunteer=True,
        )
    )
    assert got.compensation_eur is None and got.care == [] and got.refund_or_rerouting is not None
    assert article in got.articles


def test_art_7_2_denied_boarding_rerouting_reduction(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            disruption_type="denied_boarding",
            cause_code="overbooking",
            rerouting_offered=ReroutingOffer(arrival_late_minutes=120),
        )
    )
    assert got.compensation_eur == 400 and got.may_reduce_to_eur == 200


# ------------------------------------------------------------------ care


def test_amended_special_needs_care_without_threshold(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            departure_delay_minutes=45,
            arrival_delay_minutes=40,
            passenger_flags=["pregnant"],
        )
    )
    assert got.care_items == {"refreshments", "communications"} and "11(9)" in got.articles


def test_2004_hotel_is_not_capped(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            disruption_type="cancellation",
            cause_code="weather_unsafe",
            notice_given_days=0,
            nights_needed=5,
        )
    )
    assert got.hotel_nights_owed == 5 and "hotel" in got.care_items


def test_amended_hotel_cap_only_in_extraordinary_circumstances(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="cancellation",
            cause_code="technical_fault",
            notice_given_days=0,
            nights_needed=5,
        )
    )
    assert got.hotel_nights_owed == 5


def test_amended_hotel_within_cap_is_unchanged(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(
        make(
            scheduled_departure=NEW_DAY,
            booking_date="2027-10-30",
            disruption_type="cancellation",
            cause_code="weather_unsafe",
            notice_given_days=0,
            nights_needed=2,
        )
    )
    assert got.hotel_nights_owed == 2


def test_missed_connection_care_uses_first_leg_delay_without_onward_data(
    engine: EntitlementEngine,
) -> None:
    got = engine.compute_entitlement(
        make(
            disruption_type="missed_connection",
            origin_iata="FCO",
            connection_iatas=["BRU"],
            final_destination_iata="HAM",
            departure_delay_minutes=150,
            arrival_delay_minutes=200,
        )
    )
    assert got.care_items == {"meals_refreshments", "communications"}


def test_reason_trace_records_every_decision(engine: EntitlementEngine) -> None:
    got = engine.compute_entitlement(make(arrival_delay_minutes=200, departure_delay_minutes=200))
    rules = [s.rule for s in got.reason_trace]
    assert rules[:3] == ["regime", "scope", "distance"] and "delay" in rules and "care" in rules
