"""Entitlement engine (FR-4): `EntitlementEngine.compute_entitlement(case) -> Entitlement`.

All I/O (rulesets, airports, territories) happens when the engine is built;
`compute_entitlement` is a pure function of the case. Every number comes from the ruleset
YAML; the code only encodes the shape of the rules. Plain-English reasoning for each rule:
docs/regulation_notes.md.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

from flightright.models import (
    CareItem,
    CareObligation,
    DisruptionCase,
    Entitlement,
    Regime,
    RerouteRefundRights,
    RuleStep,
)
from flightright.rules.airports import Airport, AirportDirectory, Territories, great_circle_km

Rules = dict[str, Any]
Defence = Literal["none", "exempt", "review"]

AMENDED: Regime = "eu261_amended_2027"
OLD: Regime = "eu261_2004"


def compare(value: float, threshold: float, op: str) -> bool:
    """Boundary comparison named in the ruleset (gte / gt / lte / lt)."""
    if op == "gte":
        return value >= threshold
    if op == "gt":
        return value > threshold
    if op == "lte":
        return value <= threshold
    if op == "lt":
        return value < threshold
    raise ValueError(f"unknown comparison {op!r}")


_CITATION = re.compile(r"^(\d|C-\d|Annex|Recital|Guidelines|Art\.)")


def _ref(source: str) -> list[str]:
    """Split 'C-549/07 Wallentin-Hermann; C-257/14 van der Lans' into two refs.

    Text after ':' is commentary.

    Only legal citations are kept; descriptive notes ("inherent in normal activity") are dropped.
    """
    parts = (part.split(":", 1)[0].strip() for part in source.split(";"))
    return [p for p in parts if _CITATION.match(p)]


class _Run:
    """Accumulates the reason trace, articles and verification state for one evaluation."""

    def __init__(self, regime: Regime, rules: Rules) -> None:
        self.regime = regime
        self.rules = rules
        self.unverified_keys: set[str] = set(rules["unverified"])
        self.trace: list[RuleStep] = []
        self.articles: list[str] = []
        self.touched: list[str] = []
        self.review: list[str] = []

    def step(self, rule: str, result: str, *articles: str) -> None:
        self.trace.append(
            RuleStep(rule=rule, result=result, article=articles[0] if articles else None)
        )
        self.cite(*articles)

    def cite(self, *articles: str) -> None:
        for article in articles:
            if article and article not in self.articles:
                self.articles.append(article)

    def touch(self, key: str) -> None:
        """Record that an evaluation relied on this rule; unverified rules lower the confidence."""
        if key in self.unverified_keys and key not in self.touched:
            self.touched.append(key)
            self.trace.append(RuleStep(rule=key, result="relies on an unverified rule"))

    @property
    def amended(self) -> bool:
        return self.regime == AMENDED


class EntitlementEngine:
    def __init__(
        self,
        rulesets: dict[Regime, Rules],
        boundary: date,
        airports: AirportDirectory,
        territories: Territories,
    ) -> None:
        self.rulesets = rulesets
        self.boundary = boundary
        self.airports = airports
        self.territories = territories

    @classmethod
    def from_config(cls, root: Path, airports_csv: Path | None = None) -> EntitlementEngine:
        config = yaml.safe_load((root / "config" / "app_config.yaml").read_text(encoding="utf-8"))
        rulesets: dict[Regime, Rules] = {}
        for regime in (OLD, AMENDED):
            path = root / "config" / "rulesets" / f"{regime}.yaml"
            rulesets[regime] = yaml.safe_load(path.read_text(encoding="utf-8"))
        csv_path = airports_csv or root / "data" / "ourairports" / "airports.csv"
        return cls(
            rulesets=rulesets,
            boundary=date.fromisoformat(config["regime"]["boundary_date"]),
            airports=AirportDirectory.from_csv(csv_path),
            territories=Territories.from_yaml(root / "config" / "territories.yaml"),
        )

    # ------------------------------------------------------------------ public API

    def select_regime(self, case: DisruptionCase) -> Regime:
        departure_day = case.scheduled_departure.date()  # local date at the departure airport
        return AMENDED if departure_day >= self.boundary else OLD

    def compute_entitlement(self, case: DisruptionCase) -> Entitlement:
        regime = self.select_regime(case)
        run = _Run(regime, self.rulesets[regime])
        run.step(
            "regime",
            f"{regime} (scheduled departure {case.scheduled_departure.date()})",
            "Reg. 2026/2202 Art. 3" if run.amended else "",
        )
        if run.amended and case.booking_date and case.booking_date < self.boundary:
            run.step("transition", "booked before the amended rules applied; no transitional rule")
            run.touch("applies_to.booked_before_boundary")

        origin = self.airports.get(case.origin_iata)
        final = self.airports.get(case.final_destination_iata)
        if not self._in_scope(run, case, origin, final):
            return self._finish(
                run, case, in_scope=False, compensation=None, reason="not covered by the Regulation"
            )

        distance, band, intra_eu = self._distance_band(run, case, origin, final)
        disruption = case.disruption_type
        if disruption in ("delay", "cancellation") and self._brought_forward(run, case):
            disruption = "cancellation"

        if (
            disruption == "denied_boarding"
            and case.cause_code in run.rules["denied_boarding"]["not_denied_boarding_reasons"]
        ):
            run.step(
                "denied_boarding",
                f"boarding refused for {case.cause_code}: not denied boarding",
                "2(j)",
            )
            return self._finish(
                run,
                case,
                in_scope=True,
                compensation=None,
                reason="refusal on reasonable grounds is not denied boarding",
                distance=distance,
                band=band,
            )

        defence: Defence = "none"
        compensation, reason, may_reduce = None, "", None
        if disruption == "delay":
            compensation, reason, may_reduce, defence = self._delay(run, case, band)
        elif disruption == "missed_connection":
            compensation, reason, may_reduce, defence = self._missed_connection(run, case, band)
        elif disruption == "cancellation":
            compensation, reason, may_reduce, defence = self._cancellation(run, case, band)
        else:
            compensation, reason, may_reduce = self._denied_boarding(run, case, band)

        if compensation is not None:
            self._near_band_edge(run, distance, intra_eu)
        care, nights = self._care(run, case, disruption, defence)
        refund = self._refund(run, case, disruption)
        return self._finish(
            run,
            case,
            in_scope=True,
            compensation=compensation,
            reason=reason,
            may_reduce=may_reduce,
            distance=distance,
            band=band,
            care=care,
            nights=nights,
            refund=refund,
        )

    # ------------------------------------------------------------------ scope and distance

    def _in_scope(self, run: _Run, case: DisruptionCase, origin: Airport, final: Airport) -> bool:
        scope = run.rules["scope"]
        if case.fare_type in scope["excluded_fares"]:
            run.step("scope", f"{case.fare_type} is excluded", "3(3)")
            return False
        if case.disruption_type in scope.get("check_in_required_for", []) and not case.check_in_ok:
            run.step("scope", "passenger did not present for check-in in time", "3(2)(a)")
            return False
        origin_t = self._territory(run, origin)
        final_t = self._territory(run, final)
        if origin_t != "other":
            run.step(
                "scope", f"departs from {origin.iata} ({origin.country}), in the EU", "3(1)(a)"
            )
            return True
        if final_t != "other" and case.carrier_is_eu:
            run.step(
                "scope", f"arrives in the EU on an EU carrier ({case.carrier_iata})", "3(1)(b)"
            )
            return True
        run.step(
            "scope", "departs outside the EU and is not an EU carrier arriving in the EU", "3(1)"
        )
        return False

    def _territory(self, run: _Run, airport: Airport) -> str:
        territory = self.territories.classify(airport.country)
        if territory == "eea_ch":
            run.touch("scope.territory.eea_and_switzerland")
        return territory

    def _distance_band(
        self, run: _Run, case: DisruptionCase, origin: Airport, final: Airport
    ) -> tuple[float, Rules, bool]:
        radius = run.rules["distance"]["earth_radius_km"]
        distance = round(great_circle_km(origin, final, radius), 1)
        intra_eu = (
            self.territories.classify(origin.country) != "other"
            and self.territories.classify(final.country) != "other"
        )
        band = self._band(run.rules, distance, intra_eu)
        basis = "C-559/16 Bossen" if not run.amended else "7(5)"
        via = f" via {', '.join(case.connection_iatas)}" if case.connection_iatas else ""
        run.step(
            "distance",
            f"{origin.iata}->{final.iata}{via}: {distance} km direct, "
            f"{'intra-EU' if intra_eu else 'not intra-EU'} "
            f"-> band {band['id']} (EUR {band['eur']})",
            basis if case.connection_iatas or run.amended else "7(4)",
        )
        return distance, band, intra_eu

    @staticmethod
    def _band(rules: Rules, distance: float, intra_eu: bool) -> Rules:
        a, b, c = rules["compensation"]["bands"]
        if compare(distance, a["max_km"], a["max_comparison"]):
            return dict(a)
        if intra_eu and compare(distance, b["intra_eu_min_km"], b["intra_eu_min_comparison"]):
            return dict(b)
        if not intra_eu and compare(distance, b["other_max_km"], b["other_max_comparison"]):
            return dict(b)
        return dict(c)

    def _near_band_edge(self, run: _Run, distance: float, intra_eu: bool) -> None:
        tolerance = run.rules["distance"]["boundary_tolerance_km"]
        if abs(distance - 1500) <= tolerance:
            run.review.append(
                f"distance {distance} km is within {tolerance} km of the 1500 km band edge"
            )
            run.step("distance", "near the 1500 km band edge")
        if not intra_eu and abs(distance - 3500) <= tolerance:
            run.touch("compensation.boundary_3500_km")

    def _brought_forward(self, run: _Run, case: DisruptionCase) -> bool:
        limit = run.rules["cancellation"]["brought_forward_minutes_counts_as_cancellation"]
        if case.brought_forward_minutes > limit:
            run.step(
                "cancellation",
                f"departure brought forward by {case.brought_forward_minutes} min "
                f"(> {limit}) counts as a cancellation",
                "2(l)(ii)" if run.amended else "C-146/20 Azurair",
            )
            run.touch("cancellation.brought_forward_minutes_counts_as_cancellation")
            return True
        return False

    # ------------------------------------------------------------------ compensation

    def _defence(self, run: _Run, case: DisruptionCase) -> Defence:
        ec = run.rules["extraordinary_circumstances"]
        codes = ec["cause_codes"]
        code = case.cause_code if case.cause_code in codes else "unknown"
        entry = codes[code]
        run.touch(f"extraordinary_circumstances.cause_codes.{code}")
        run.cite(*(["6c", "Annex", "2(r)"] if run.amended else ["5(3)"]), *_ref(entry["source"]))
        if entry["class"] == "extraordinary":
            run.step("extraordinary_circumstances", f"{code}: extraordinary, compensation not owed")
            return "exempt"
        if entry["class"] == "review":
            run.review.append(f"carrier defence for cause '{code}' needs review")
            run.step(
                "extraordinary_circumstances", f"{code}: depends on facts the engine cannot judge"
            )
            return "review"
        run.step("extraordinary_circumstances", f"{code}: not extraordinary")
        return "none"

    @staticmethod
    def _apply_defence(defence: Defence, eur: int, reason: str) -> tuple[int | None, str]:
        if defence == "exempt":
            return (
                None,
                "extraordinary circumstances: no compensation (care and refund rights remain)",
            )
        if defence == "review":
            return (
                None,
                "possible extraordinary circumstances: needs review before stating an amount",
            )
        return eur, reason

    def _delay(
        self, run: _Run, case: DisruptionCase, band: Rules
    ) -> tuple[int | None, str, int | None, Defence]:
        rule = run.rules["compensation"]["delay"]
        late = case.arrival_delay_minutes or 0
        triggered = compare(late, rule["trigger_minutes"], rule["comparison"])
        run.step(
            "delay",
            f"arrival {late} min late at the final destination: "
            f"{'meets' if triggered else 'below'} the {rule['trigger_minutes']} min "
            f"({rule['comparison']}) trigger",
            *rule["articles"],
        )
        if not triggered:
            return None, "arrival delay below the compensation threshold", None, "none"
        return self._arrival_compensation(run, case, band, late, "delay")

    def _arrival_compensation(
        self, run: _Run, case: DisruptionCase, band: Rules, late: int, kind: str
    ) -> tuple[int | None, str, int | None, Defence]:
        defence = self._defence(run, case)
        eur, reason = self._apply_defence(defence, band["eur"], f"{kind} at the final destination")
        run.cite(band["article"])
        may_reduce = None
        if eur is not None:
            may_reduce = self._reduction(run, band, late, kind)
        return eur, reason, may_reduce, defence

    def _reduction(self, run: _Run, band: Rules, late: int, kind: str) -> int | None:
        red = run.rules["compensation"]["reduction_on_rerouting"]
        if run.amended:
            if (
                band["id"] in red["bands"]
                and kind in red["applies_to"]
                and compare(late, red["arrival_delay_max_minutes"], red["comparison"])
            ):
                run.step(
                    "reduction", f"arrival {late} min late: carrier may reduce by 50 %", "7(2)"
                )
                return int(band["eur"] * red["factor"])
            return None
        if kind in ("delay", "missed_connection"):
            long_haul = red["delay_long_haul"]
            if band["id"] == long_haul["band"] and late <= long_haul["arrival_delay_max_minutes"]:
                run.step(
                    "reduction", f"long-haul delay of {late} min: carrier may pay 50 %", "7(2)(c)"
                )
                run.touch("compensation.reduction_on_rerouting.delay_long_haul")
                return int(band["eur"] * red["factor"])
            return None
        if kind in red["applies_to"] and compare(
            late, red["arrival_delay_max_minutes"][band["id"]], red["comparison"]
        ):
            run.step(
                "reduction",
                f"rerouted arrival {late} min late: carrier may reduce by 50 %",
                f"7(2)({band['id']})",
            )
            return int(band["eur"] * red["factor"])
        return None

    def _missed_connection(
        self, run: _Run, case: DisruptionCase, band: Rules
    ) -> tuple[int | None, str, int | None, Defence]:
        late = case.arrival_delay_minutes or 0
        if run.amended:
            mc = run.rules["missed_connection"]
            if not case.single_booking:
                run.step(
                    "missed_connection", "separate contracts: Arts 7, 8, 9 do not apply", "14(3)"
                )
                return (
                    None,
                    "separate tickets: no rights for the missed onward flight",
                    None,
                    "none",
                )
            run.touch("missed_connection.responsible_carrier")
            triggered = compare(late, mc["compensation_if_arrival_delay_minutes"], mc["comparison"])
            run.step(
                "missed_connection",
                f"single contract, {late} min late at the final destination",
                "6b(1)",
                "6b(3)",
            )
        else:
            if not case.single_booking:
                run.review.append("separate tickets: only the disrupted flight can be assessed")
                run.step("missed_connection", "separate tickets: assess the disrupted flight only")
                return None, "separate tickets: needs review of the disrupted flight", None, "none"
            rule = run.rules["compensation"]["delay"]
            triggered = compare(late, rule["trigger_minutes"], rule["comparison"])
            run.step(
                "missed_connection",
                f"single booking: delay of {late} min at the final destination",
                "C-11/11 Folkerts",
                *rule["articles"],
            )
        if not triggered:
            return None, "arrival delay below the compensation threshold", None, "none"
        return self._arrival_compensation(run, case, band, late, "missed_connection")

    def _cancellation(
        self, run: _Run, case: DisruptionCase, band: Rules
    ) -> tuple[int | None, str, int | None, Defence]:
        canc = run.rules["cancellation"]
        notice = case.notice_given_days
        run.cite(*canc["articles"])
        if notice is None:
            run.step("cancellation", "notice date unknown: the carrier bears the burden of proof")
        else:
            if not run.amended and notice == 7:
                run.touch("cancellation.notice_exactly_7_days")
            for rule in canc["notice_rules"]:
                if self._notice_rule_matches(run, case, rule, notice):
                    return None, f"notice rule {rule['id']}: no compensation", None, "none"
        defence = self._defence(run, case)
        eur, reason = self._apply_defence(
            defence, band["eur"], "cancellation without sufficient notice"
        )
        run.cite(band["article"])
        may_reduce = None
        offer = case.rerouting_offered
        if eur is not None and offer is not None:
            candidates = [
                r
                for r in (
                    self._reduction(run, band, offer.arrival_late_minutes, "cancellation"),
                    self._earlier_departure(run, case, band),
                )
                if r is not None
            ]
            may_reduce = min(candidates) if candidates else None
        return eur, reason, may_reduce, defence

    @staticmethod
    def _notice_rule_matches(run: _Run, case: DisruptionCase, rule: Rules, notice: int) -> bool:
        if notice < rule.get("notice_days_min", 0):
            return False
        if "notice_days_max_exclusive" in rule and notice >= rule["notice_days_max_exclusive"]:
            return False
        if "reroute_departure_early_max_minutes" not in rule:
            run.step(
                "cancellation", f"informed {notice} days ahead: no compensation", rule["article"]
            )
            return True
        offer = case.rerouting_offered
        ok = (
            offer is not None
            and compare(
                offer.departure_early_minutes,
                rule["reroute_departure_early_max_minutes"],
                rule["departure_comparison"],
            )
            and compare(
                offer.arrival_late_minutes,
                rule["reroute_arrival_late_max_minutes"],
                rule["arrival_comparison"],
            )
        )
        detail = (
            f"rerouted {offer.departure_early_minutes} min early, "
            f"{offer.arrival_late_minutes} min late"
            if offer
            else "no rerouting offered"
        )
        run.step(
            "cancellation",
            f"informed {notice} days ahead, {detail}: "
            f"{'inside' if ok else 'outside'} the rerouting window",
            rule["article"],
        )
        return ok

    def _earlier_departure(self, run: _Run, case: DisruptionCase, band: Rules) -> int | None:
        if not run.amended:
            return None
        offer = case.rerouting_offered
        if offer is None or not offer.taken:
            return None
        rule = run.rules["compensation"]["earlier_departure_reduction"]
        for index, tier in enumerate(rule["tiers"]):
            early = offer.departure_early_minutes
            if early > tier["brought_forward_min_minutes"] and compare(
                early, tier["brought_forward_max_minutes"], tier["max_comparison"]
            ):
                run.touch("compensation.earlier_departure_reduction")
                run.step(
                    "reduction",
                    f"rerouted flight taken {early} min early: compensation x {tier['factor']}",
                    f"7(3)({'ab'[index]})",
                )
                return int(band["eur"] * tier["factor"])
        return None

    def _denied_boarding(
        self, run: _Run, case: DisruptionCase, band: Rules
    ) -> tuple[int | None, str, int | None]:
        if case.volunteer:
            run.step(
                "denied_boarding",
                "volunteered in exchange for agreed benefits",
                "4(2)" if run.amended else "4(1)",
            )
            return None, "volunteer: agreed benefits replace compensation", None
        run.step(
            "denied_boarding",
            "denied boarding against the passenger's will",
            "4(4)" if run.amended else "4(3)",
            band["article"],
            "8",
            "9",
        )
        may_reduce = None
        if case.rerouting_offered is not None:
            may_reduce = self._reduction(
                run, band, case.rerouting_offered.arrival_late_minutes, "denied_boarding"
            )
        return (
            band["eur"],
            "involuntary denied boarding (no extraordinary-circumstances defence)",
            may_reduce,
        )

    # ------------------------------------------------------------------ care and refunds

    def _care(
        self, run: _Run, case: DisruptionCase, disruption: str, defence: Defence
    ) -> tuple[list[CareObligation], int | None]:
        care_rules = run.rules["care"]
        items: list[tuple[CareItem, str, str]] = []
        wait = case.departure_delay_minutes
        if (
            disruption == "missed_connection"
            and case.connection_departure_delay_minutes is not None
        ):
            wait = case.connection_departure_delay_minutes
        special = bool(set(case.passenger_flags) & set(run.rules["special_needs"]["categories"]))

        if disruption == "denied_boarding":
            if not case.volunteer:
                art = care_rules["denied_boarding"]["article"]
                food: CareItem = "refreshments_immediate" if run.amended else "meals_refreshments"
                items += [
                    (food, "refreshments straight away", art),
                    ("communications", "calls / e-mails", art),
                ]
        elif disruption == "cancellation":
            rule = care_rules["cancellation"]
            notice = case.notice_given_days
            if notice is None or notice <= rule["when_notice_days_max"]:
                items += self._food_items(run, None, rule["article"])
        elif run.amended:
            if wait >= care_rules["items"]["refreshments"]["every_minutes_of_waiting"]:
                art = "6b(1)" if disruption == "missed_connection" else "6(2)"
                items += self._food_items(run, wait, art)
        else:
            sector_band = self._sector_band(run, case, disruption)
            trigger = care_rules["delay_triggers_minutes"][sector_band]
            if compare(wait, trigger, care_rules["delay_triggers_minutes"]["comparison"]):
                items += self._food_items(run, wait, f"6(1)({sector_band})")

        if not items and special and wait > 0:
            art = "11(9)" if run.amended else "11(2)"
            food = "refreshments" if run.amended else "meals_refreshments"
            items += [
                (food, "as soon as possible (special needs)", art),
                ("communications", "as soon as possible (special needs)", art),
            ]
            run.step("care", "special-needs passenger: care as soon as possible", art)

        nights = None
        if items and case.nights_needed > 0:
            hotel = care_rules["items"]["hotel"]
            nights = case.nights_needed
            run.cite(hotel["article"])
            cap = hotel.get("cap_nights_extraordinary")
            if cap is not None and defence == "exempt":
                if special:
                    run.step(
                        "care", "hotel cap does not apply to special-needs passengers", "11(10)"
                    )
                elif nights > cap:
                    nights = cap
                    run.step(
                        "care", f"extraordinary circumstances: hotel capped at {cap} nights", "9(6)"
                    )
            items += [
                ("hotel", f"{nights} night(s)", hotel["article"]),
                ("hotel_transport", "to and from the hotel", hotel["article"]),
            ]

        if items:
            run.cite("9(1)" if run.amended else "9")
            if defence == "exempt" and not run.amended:
                run.cite("C-12/11 McDonagh")
        return [CareObligation(item=i, detail=d, article=a) for i, d, a in items], nights

    @staticmethod
    def _food_items(run: _Run, wait: int | None, article: str) -> list[tuple[CareItem, str, str]]:
        run.step(
            "care",
            f"right to care ({f'waiting {wait} min' if wait is not None else 'same-day cancel'})",
            article,
        )
        if not run.amended:
            return [
                ("meals_refreshments", "in reasonable relation to the waiting time", article),
                ("communications", "two calls or e-mails", article),
            ]
        meals = run.rules["care"]["items"]["meals"]
        out: list[tuple[CareItem, str, str]] = [("refreshments", "every 2 h of waiting", article)]
        if wait is None or wait >= meals["first_after_minutes"]:
            out.append(("meals", "after 3 h, then every 5 h (max 3 a day)", article))
        out.append(("communications", "internet access and two calls", article))
        return out

    def _sector_band(self, run: _Run, case: DisruptionCase, disruption: str) -> str:
        """2004 care thresholds use the distance of the delayed (or onward) flight itself."""
        if disruption == "missed_connection" and case.connection_iatas:
            start, end = case.connection_iatas[-1], case.final_destination_iata
        else:
            start, end = case.origin_iata, case.destination_iata or case.final_destination_iata
        a, b = self.airports.get(start), self.airports.get(end)
        intra = (
            self.territories.classify(a.country) != "other"
            and self.territories.classify(b.country) != "other"
        )
        radius = run.rules["distance"]["earth_radius_km"]
        return str(self._band(run.rules, great_circle_km(a, b, radius), intra)["id"])

    def _refund(
        self, run: _Run, case: DisruptionCase, disruption: str
    ) -> RerouteRefundRights | None:
        rr = run.rules["refund_or_rerouting"]
        if disruption in ("cancellation", "denied_boarding"):
            run.cite("8(1)" if disruption == "cancellation" else "8")
            return RerouteRefundRights(
                refund_within_days=rr["refund_within_days"],
                options=list(rr["options"]),
                detail="refund, rerouting at the earliest opportunity, or a later date",
            )
        wait = case.departure_delay_minutes
        if disruption == "missed_connection":
            if run.amended and not case.single_booking:
                return None
            wait = case.connection_departure_delay_minutes or wait
        limit = run.rules["care"]["delay_refund_option_minutes"]
        if wait >= limit:
            arts = ["6(3)", "8(1)"] if run.amended else ["6(1)(iii)", "8(1)(a)"]
            if disruption == "missed_connection" and run.amended:
                arts = ["6b(2)", "8(1)"]
            run.step("refund", f"departure delayed {wait} min (>= {limit}): refund option", *arts)
            return RerouteRefundRights(
                refund_within_days=rr["refund_within_days"],
                options=["refund"],
                detail="refund instead of travelling (return flight if no longer useful)",
            )
        return None

    # ------------------------------------------------------------------ result

    def _finish(
        self,
        run: _Run,
        case: DisruptionCase,
        *,
        in_scope: bool,
        compensation: int | None,
        reason: str,
        may_reduce: int | None = None,
        distance: float | None = None,
        band: Rules | None = None,
        care: list[CareObligation] | None = None,
        nights: int | None = None,
        refund: RerouteRefundRights | None = None,
    ) -> Entitlement:
        rules = run.rules
        deadline = None
        voucher = None
        if in_scope:
            voucher = rules["vouchers"]["requires"]
            run.cite("8a(2)" if run.amended else "7(3)")
            deadline = rules["claim_deadline"]["in_regulation"]
            if run.amended:
                run.cite("7(9)")
                if compensation is not None:
                    run.cite(rules["compensation"]["payment"]["article"])
        unverified = bool(run.touched or run.review)
        return Entitlement(
            regime=run.regime,
            ruleset_version=rules["version"],
            in_scope=in_scope,
            compensation_eur=compensation,
            compensation_reason=reason,
            may_reduce_to_eur=may_reduce,
            distance_km=distance,
            band=band["id"] if band else None,
            care=care or [],
            hotel_nights_owed=nights,
            refund_or_rerouting=refund,
            voucher_requires=voucher,
            claim_deadline=deadline,
            articles=run.articles,
            reason_trace=run.trace,
            unverified_rules=run.touched,
            review_reasons=run.review,
            confidence="unverified" if unverified else "verified",
        )
