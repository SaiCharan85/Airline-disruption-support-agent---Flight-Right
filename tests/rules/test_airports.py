from pathlib import Path

import pytest

from flightright.rules.airports import (
    Airport,
    AirportDirectory,
    Territories,
    UnknownAirportError,
    great_circle_km,
)

ROOT = Path(__file__).resolve().parents[2]


def test_FR_4_4_great_circle_matches_known_distance() -> None:
    directory = AirportDirectory.from_csv(ROOT / "tests" / "fixtures" / "airports.csv")
    # Bossen C-559/16: Rome -> Hamburg is about 1,326 km direct.
    assert great_circle_km(directory.get("FCO"), directory.get("HAM")) == pytest.approx(1326, abs=5)
    assert len(directory) >= 20


def test_lookup_is_case_insensitive_and_unknown_raises() -> None:
    directory = AirportDirectory.from_csv(ROOT / "tests" / "fixtures" / "airports.csv")
    assert directory.get("mad").country == "ES"
    with pytest.raises(UnknownAirportError):
        directory.get("ZZZ")


def test_duplicate_iata_prefers_large_airport(tmp_path: Path) -> None:
    csv_file = tmp_path / "a.csv"
    csv_file.write_text(
        "ident,type,name,latitude_deg,longitude_deg,iso_country,iata_code\n"
        "X1,small_airport,Small,1,1,DE,ABC\n"
        "X2,large_airport,Large,2,2,DE,ABC\n"
        "X3,heliport,No code,0,0,DE,\n",
        encoding="utf-8",
    )
    directory = AirportDirectory.from_csv(csv_file)
    assert directory.get("ABC").name == "Large" and len(directory) == 1


def test_territories() -> None:
    t = Territories.from_yaml(ROOT / "config" / "territories.yaml")
    assert t.classify("PT") == "eu" and t.classify("RE") == "eu"
    assert t.classify("NO") == "eea_ch" and t.classify("GB") == "other"


def test_unquoted_country_code_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "t.yaml"
    bad.write_text(
        "eu_member_states: [DE]\neu_outermost_regions: []\neea_and_switzerland: [NO]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="quote them"):
        Territories.from_yaml(bad)


def test_zero_distance() -> None:
    a = Airport("AAA", "A", 10.0, 10.0, "DE")
    assert great_circle_km(a, a) == 0
