"""
US macroeconomic indicators via FRED (Federal Reserve Economic Data),
the St. Louis Fed's free public API.

US-only, and deliberately so: FRED doesn't cover Nigeria, and no other
source is wired up for Nigerian macro data yet. Don't extend this file
to fake NGX-side numbers — see EconomicIndicator's docstring in models.py.

Series pulled (verify against https://fred.stlouisfed.org/series/<id>
before changing — FRED occasionally revises/discontinues series IDs):
- GDPC1: Real Gross Domestic Product (chained dollars). Deliberately the
  *real* series, not nominal GDP ("GDP") -- "GDP growth" conventionally
  means inflation-adjusted growth, and reporting nominal GDP under that
  label would overstate growth by however much of it is just inflation.
- CPIAUCSL: Consumer Price Index for All Urban Consumers (inflation)
- UNRATE: Unemployment Rate
- FEDFUNDS: Federal Funds Effective Rate

name/unit are never hardcoded here -- both come from FRED's own series
metadata endpoint, so a series description changing upstream doesn't
require a code change here to stay accurate.

Run as a scheduled job, e.g.:
    python fred_puller.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from time import sleep

import requests

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session          # noqa: E402
from models import EconomicIndicator       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fred_puller")

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_BASE_URL = "https://api.stlouisfed.org/fred"
MAX_RETRIES = 3
BACKOFF_SECONDS = 5

# {our series_code: FRED series ID} -- currently the same, kept as a
# separate mapping in case a series ID gets revised upstream and we want
# our own code to stay stable across that.
SERIES = {
    "GDPC1": "GDPC1",
    "CPIAUCSL": "CPIAUCSL",
    "UNRATE": "UNRATE",
    "FEDFUNDS": "FEDFUNDS",
}

# How many recent observations to pull per series per run. Not a daily
# feed -- see the workflow's schedule -- so this backfills a bit of real
# trend history rather than leaving the frontend with a single point after
# the first run.
OBSERVATIONS_PER_SERIES = 24


def _get_with_retry(path: str, params: dict) -> dict | None:
    url = f"{FRED_BASE_URL}/{path}"
    params = {**params, "api_key": FRED_API_KEY, "file_type": "json"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=20)
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, path, exc)
            sleep(BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code == 200:
            return resp.json()
        log.warning("Unexpected status %s for %s (attempt %d/%d): %s", resp.status_code, path, attempt, MAX_RETRIES, resp.text[:300])
        sleep(BACKOFF_SECONDS * attempt)

    log.error("Giving up on %s after %d attempts", path, MAX_RETRIES)
    return None


def fetch_series_metadata(fred_series_id: str) -> dict | None:
    """Real title/units straight from FRED, not hardcoded -- see module
    docstring for why."""
    data = _get_with_retry("series", {"series_id": fred_series_id})
    if not data or not data.get("seriess"):
        return None
    info = data["seriess"][0]
    return {"title": info["title"], "units": info["units"]}


def fetch_observations(fred_series_id: str) -> list[dict]:
    data = _get_with_retry("series/observations", {
        "series_id": fred_series_id,
        "sort_order": "desc",
        "limit": OBSERVATIONS_PER_SERIES,
    })
    if not data:
        return []
    # FRED prints "." for a not-yet-available observation at the requested
    # date rather than omitting it -- skip those rather than storing a
    # fabricated 0.
    return [
        {"date": obs["date"], "value": float(obs["value"])}
        for obs in data.get("observations", [])
        if obs.get("value") not in (None, ".")
    ]


def upsert_indicator(db, series_code: str, name: str, unit: str, observations: list[dict]) -> int:
    upserted = 0
    for obs in observations:
        obs_date = datetime.strptime(obs["date"], "%Y-%m-%d").date()
        existing = (
            db.query(EconomicIndicator)
            .filter(EconomicIndicator.series_code == series_code, EconomicIndicator.date == obs_date)
            .one_or_none()
        )
        if existing:
            existing.value = obs["value"]
            existing.name = name
            existing.unit = unit
        else:
            db.add(EconomicIndicator(
                series_code=series_code, name=name, value=obs["value"],
                date=obs_date, country="US", unit=unit,
            ))
        upserted += 1
    return upserted


def run() -> None:
    if not FRED_API_KEY:
        log.warning("FRED_API_KEY not set — skipping economic indicator pull")
        return

    with get_session() as db:
        for our_code, fred_id in SERIES.items():
            metadata = fetch_series_metadata(fred_id)
            if metadata is None:
                log.error("Skipping %s — couldn't fetch series metadata", our_code)
                continue

            observations = fetch_observations(fred_id)
            if not observations:
                log.warning("No observations returned for %s", our_code)
                continue

            count = upsert_indicator(db, our_code, metadata["title"], metadata["units"], observations)
            log.info("Upserted %d observations for %s (%s)", count, our_code, metadata["title"])


if __name__ == "__main__":
    run()
