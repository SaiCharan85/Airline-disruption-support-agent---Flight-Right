from pathlib import Path

import pytest

from flightright.rules.engine import EntitlementEngine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def engine() -> EntitlementEngine:
    """Engine on the committed airport fixture (no full OurAirports download needed)."""
    return EntitlementEngine.from_config(
        ROOT, airports_csv=ROOT / "tests" / "fixtures" / "airports.csv"
    )
