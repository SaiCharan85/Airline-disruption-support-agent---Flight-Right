"""Golden entitlement cases (SPEC §9.2): hand-worked expectations in cases.yaml."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from flightright.models import DisruptionCase
from flightright.rules.engine import EntitlementEngine

CASES: list[dict[str, Any]] = yaml.safe_load(
    (Path(__file__).parent / "cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("golden", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(engine: EntitlementEngine, golden: dict[str, Any]) -> None:
    case = DisruptionCase.model_validate(golden["case"])
    got = engine.compute_entitlement(case)
    exp = golden["expected"]
    trace = "\n".join(f"  {s.rule}: {s.result}" for s in got.reason_trace)
    assert got.regime == exp["regime"], trace
    assert got.in_scope == exp["in_scope"], trace
    assert got.compensation_eur == exp["compensation_eur"], trace
    assert got.may_reduce_to_eur == exp.get("may_reduce_to_eur"), trace
    assert got.care_items == set(exp.get("care", [])), trace
    assert (got.refund_or_rerouting is not None) == exp.get("refund_option", False), trace
    assert got.confidence == exp["confidence"], (
        f"{got.unverified_rules} {got.review_reasons}\n{trace}"
    )
    missing = [a for a in exp["articles"] if a not in got.articles]
    assert not missing, f"missing articles {missing}; got {got.articles}"
    if "hotel_nights_owed" in exp:
        assert got.hotel_nights_owed == exp["hotel_nights_owed"], trace
    if "voucher_requires" in exp:
        assert got.voucher_requires == exp["voucher_requires"]
    if "claim_deadline" in exp:
        assert got.claim_deadline and exp["claim_deadline"] in got.claim_deadline
