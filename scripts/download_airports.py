"""Download the latest OurAirports airports.csv (public domain) and record a manifest.

  uv run python scripts/download_airports.py            # -> data/ourairports/ (csv + manifest)
  uv run python scripts/download_airports.py --fixture    # also refresh tests/fixtures/airports.csv

The fixture is a small subset (airports used by tests) so CI never needs the full download.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "ourairports"
FIXTURE = ROOT / "tests" / "fixtures" / "airports.csv"
COLUMNS = ["ident", "type", "name", "latitude_deg", "longitude_deg", "iso_country", "iata_code"]
FIXTURE_IATA = sorted(
    {
        "MAD",
        "BCN",
        "FRA",
        "LIS",
        "CDG",
        "JFK",
        "FCO",
        "BRU",
        "HAM",
        "AMS",
        "IST",
        "MUC",
        "DXB",
        "LHR",
        "ATH",
        "DUB",
        "VIE",
        "OSL",
        "ZRH",
        "PDL",
        "RUN",
        "LPA",
        "KEF",
        "WAW",
        "LCA",
        "HEL",
    }
)


def download() -> bytes:
    req = urllib.request.Request(URL, headers={"User-Agent": "flightright-data/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data: bytes = resp.read()
    return data


def write_fixture(raw: bytes) -> int:
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    keep = [{c: r[c] for c in COLUMNS} for r in rows if r["iata_code"] in FIXTURE_IATA]
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(keep, key=lambda r: r["iata_code"]))
    return len(keep)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", action="store_true", help="also rewrite the test fixture")
    args = parser.parse_args()
    raw = download()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "airports.csv").write_bytes(raw)
    sha256 = hashlib.sha256(raw).hexdigest()
    manifest = {
        "source": URL,
        "license": "Public domain (OurAirports)",
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": sha256,
        "bytes": len(raw),
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"airports.csv: {len(raw):,} bytes, sha256 {sha256[:12]}")
    if args.fixture:
        print(f"fixture: {write_fixture(raw)} airports -> {FIXTURE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
