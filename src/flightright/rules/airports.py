"""Airport coordinates (OurAirports), great-circle distance, and EU territory (FR-4.4)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Literal

import yaml

Territory = Literal["eu", "eea_ch", "other"]


class UnknownAirportError(KeyError):
    pass


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    lat: float
    lon: float
    country: str


def great_circle_km(a: Airport, b: Airport, radius_km: float = 6371.0) -> float:
    """Haversine distance between two airports."""
    lat1, lon1, lat2, lon2 = map(radians, (a.lat, a.lon, b.lat, b.lon))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * radius_km * asin(sqrt(h))


class AirportDirectory:
    def __init__(self, airports: dict[str, Airport]) -> None:
        self._by_iata = airports

    @classmethod
    def from_csv(cls, path: Path) -> AirportDirectory:
        airports: dict[str, Airport] = {}
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                code = row["iata_code"].strip()
                if len(code) != 3:
                    continue
                airport = Airport(
                    iata=code,
                    name=row["name"],
                    lat=float(row["latitude_deg"]),
                    lon=float(row["longitude_deg"]),
                    country=row["iso_country"],
                )
                # Prefer the larger airport when an IATA code is (rarely) duplicated.
                if code not in airports or row.get("type") == "large_airport":
                    airports[code] = airport
        return cls(airports)

    def get(self, iata: str) -> Airport:
        try:
            return self._by_iata[iata.upper()]
        except KeyError:
            raise UnknownAirportError(iata) from None

    def __len__(self) -> int:
        return len(self._by_iata)


class Territories:
    def __init__(self, eu: set[str], eea_ch: set[str]) -> None:
        self._eu = eu
        self._eea_ch = eea_ch

    @classmethod
    def from_yaml(cls, path: Path) -> Territories:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        groups = ("eu_member_states", "eu_outermost_regions", "eea_and_switzerland")
        for group in groups:
            bad = [c for c in data[group] if not (isinstance(c, str) and len(c) == 2)]
            if bad:  # e.g. an unquoted NO parsed as False
                raise ValueError(f"{group}: invalid country codes {bad!r} (quote them)")
        eu = set(data["eu_member_states"]) | set(data["eu_outermost_regions"])
        return cls(eu=eu, eea_ch=set(data["eea_and_switzerland"]))

    def classify(self, country: str) -> Territory:
        if country in self._eu:
            return "eu"
        if country in self._eea_ch:
            return "eea_ch"
        return "other"
