"""Structural checks on the ruleset YAMLs and golden cases (FR-4.3, FR-4.7, SPEC §9.2)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RULESETS = {
    "eu261_2004": ROOT / "config" / "rulesets" / "eu261_2004.yaml",
    "eu261_amended_2027": ROOT / "config" / "rulesets" / "eu261_amended_2027.yaml",
}
GOLDEN = ROOT / "tests" / "golden" / "cases.yaml"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _resolve(tree: Any, dotted: str) -> Any:
    node = tree
    for part in dotted.split("."):
        assert isinstance(node, dict) and part in node, f"{dotted}: missing '{part}'"
        node = node[part]
    return node


@pytest.fixture(scope="module", params=list(RULESETS))
def ruleset(request: pytest.FixtureRequest) -> dict[str, Any]:
    data = _load(RULESETS[request.param])
    assert data["ruleset"] == request.param
    return data


def test_FR_4_3_ruleset_has_versioned_core_sections(ruleset: dict[str, Any]) -> None:
    for key in (
        "version",
        "applies_to",
        "sources",
        "scope",
        "distance",
        "compensation",
        "cancellation",
        "extraordinary_circumstances",
        "care",
        "refund_or_rerouting",
        "vouchers",
        "claim_deadline",
        "unverified",
    ):
        assert key in ruleset, key


def test_FR_4_7_every_unverified_key_points_at_a_rule(ruleset: dict[str, Any]) -> None:
    for dotted in ruleset["unverified"]:
        _resolve(ruleset, dotted)


def test_FR_4_3_compensation_bands_ascend(ruleset: dict[str, Any]) -> None:
    amounts = [band["eur"] for band in ruleset["compensation"]["bands"]]
    assert amounts == sorted(amounts) and amounts[0] > 0


def test_FR_4_2_regime_boundary_matches_app_config() -> None:
    boundary = _load(ROOT / "config" / "app_config.yaml")["regime"]["boundary_date"]
    old = _load(RULESETS["eu261_2004"])["applies_to"]
    new = _load(RULESETS["eu261_amended_2027"])["applies_to"]
    assert old["scheduled_departure_before"] == boundary
    assert new["scheduled_departure_from"] == boundary


def test_extraordinary_circumstances_never_remove_care(ruleset: dict[str, Any]) -> None:
    assert "care" in ruleset["extraordinary_circumstances"]["never_removes"]


GOLDEN_REQUIRED = {"id", "title", "case", "expected"}
CASE_REQUIRED = {
    "origin_iata",
    "final_destination_iata",
    "carrier_iata",
    "carrier_is_eu",
    "scheduled_departure",
    "disruption_type",
    "cause_code",
}
EXPECTED_REQUIRED = {"regime", "in_scope", "compensation_eur", "confidence", "articles"}


def test_golden_cases_are_well_formed() -> None:
    cases = _load(GOLDEN)["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate golden case ids"
    for c in cases:
        assert c.keys() >= GOLDEN_REQUIRED, c.get("id")
        assert c["case"].keys() >= CASE_REQUIRED, c["id"]
        assert c["expected"].keys() >= EXPECTED_REQUIRED, c["id"]
        assert c["expected"]["regime"] in RULESETS, c["id"]
        assert c["expected"]["confidence"] in {"verified", "unverified"}, c["id"]


def test_golden_cases_cover_both_regimes_and_all_disruption_types() -> None:
    cases = _load(GOLDEN)["cases"]
    assert {c["expected"]["regime"] for c in cases} == set(RULESETS)
    assert {c["case"]["disruption_type"] for c in cases} == {
        "delay",
        "cancellation",
        "denied_boarding",
        "missed_connection",
    }
