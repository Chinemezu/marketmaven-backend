"""
SEC EDGAR ingestion.

EDGAR is free and official, which makes it the most stable source in
this whole pipeline — lean on it. Two things it requires that other
free APIs don't:

1. A descriptive User-Agent header identifying the app and a contact
   email. SEC blocks generic/browser-spoofed User-Agents — set
   SEC_USER_AGENT as an env var before running this in anything but a
   local test.
2. Self-imposed rate limiting. SEC's stated ceiling is ~10 req/sec;
   this stays well under that on purpose rather than testing the
   limit.

Two entry points:
- pull_company_filings(cik): recent filings list for one company, via
  the submissions API.
- search_full_text(query, forms, date_from, date_to): EDGAR full-text
  search, useful for discovering filings you don't already have a CIK
  for.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from time import sleep

import requests

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session          # noqa: E402
from models import Issuer, Filing          # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("edgar_client")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
REQUEST_DELAY_SECONDS = 0.2  # ~5 req/sec, comfortably under SEC's ceiling

# MVP starting set — tuned to the sectors NGX benchmarking cares about
# most (banking, telecoms, consumer goods, energy). Edit as coverage grows.
TRACKED_US_TICKERS = ["JPM", "BAC", "WFC", "T", "VZ", "KO", "PG", "XOM", "CVX"]

USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "Marketmaven contact@marketmaven.example",  # replace before running against real SEC servers
)


def _headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def pull_company_filings(cik: int, limit: int = 25) -> list[dict]:
    """Recent filings for one company via the submissions API. `cik`
    is SEC's numeric company identifier — Marketmaven will need a
    ticker-to-CIK lookup as a separate enrichment step; EDGAR's own
    company_tickers.json is the standard source for that mapping."""
    url = SUBMISSIONS_URL.format(cik=cik)
    resp = requests.get(url, headers=_headers(), timeout=20)
    sleep(REQUEST_DELAY_SECONDS)

    if resp.status_code != 200:
        log.warning("EDGAR submissions lookup failed for CIK %s: HTTP %s", cik, resp.status_code)
        return []

    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for i in range(min(limit, len(forms))):
        accession_clean = accession_numbers[i].replace("-", "")
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession_clean}/{primary_docs[i]}"
        )
        filings.append({
            "form": forms[i],
            "filing_date": dates[i],
            "url": doc_url,
        })
    return filings


def search_full_text(query: str, forms: str | None = None,
                      date_from: date | None = None, date_to: date | None = None,
                      limit: int = 20) -> list[dict]:
    """EDGAR full-text search — useful for pulling filings that
    mention a specific company/topic without needing its CIK first."""
    params = {"q": query, "forms": forms}
    if date_from:
        params["dateRange"] = "custom"
        params["startdt"] = date_from.isoformat()
        params["enddt"] = (date_to or date.today()).isoformat()

    resp = requests.get(FULL_TEXT_SEARCH_URL, headers=_headers(), params=params, timeout=20)
    sleep(REQUEST_DELAY_SECONDS)

    if resp.status_code != 200:
        log.warning("EDGAR full-text search failed: HTTP %s", resp.status_code)
        return []

    hits = resp.json().get("hits", {}).get("hits", [])
    return [
        {
            "form": h["_source"].get("form"),
            "filing_date": h["_source"].get("file_date"),
            "entity": h["_source"].get("display_names", [None])[0],
            "url": f"https://www.sec.gov/Archives/edgar/data/{h['_id']}",
        }
        for h in hits[:limit]
    ]


def store_filings(issuer_id: int, filings: list[dict]) -> int:
    """Upserts filings by (issuer_id, url) — url is the natural
    dedup key since accession numbers are unique per filing."""
    stored = 0
    with get_session() as db:
        for f in filings:
            exists = (
                db.query(Filing)
                .filter(Filing.issuer_id == issuer_id, Filing.url == f["url"])
                .one_or_none()
            )
            if exists:
                continue
            db.add(Filing(
                issuer_id=issuer_id,
                source="EDGAR",
                filing_type=f.get("form"),
                filing_date=datetime.strptime(f["filing_date"], "%Y-%m-%d").date(),
                url=f["url"],
            ))
            stored += 1
    log.info("Stored %d new EDGAR filings for issuer_id=%s", stored, issuer_id)
    return stored


def run_for_all_us_issuers(cik_by_issuer_id: dict[int, int]) -> None:
    """cik_by_issuer_id: {issuer.id: cik} — the CIK mapping isn't
    stored on Issuer in the current schema; pass it in from wherever
    the ticker→CIK enrichment step lands, or add a `cik` column to
    Issuer if this becomes a permanent lookup."""
    for issuer_id, cik in cik_by_issuer_id.items():
        filings = pull_company_filings(cik)
        if filings:
            store_filings(issuer_id, filings)


def load_ticker_to_cik_map() -> dict[str, int]:
    """SEC serves this as {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
    — reshape into {ticker: cik} once per run. This is the mapping
    run_for_tracked_tickers() uses so the scheduled job doesn't need
    CIKs hand-maintained anywhere."""
    resp = requests.get(TICKER_MAP_URL, headers=_headers(), timeout=20)
    sleep(REQUEST_DELAY_SECONDS)
    if resp.status_code != 200:
        log.error("Could not load SEC ticker->CIK map (HTTP %s) — aborting run", resp.status_code)
        return {}
    raw = resp.json()
    return {entry["ticker"]: int(entry["cik_str"]) for entry in raw.values()}


def run_for_tracked_tickers(tickers: list[str] = TRACKED_US_TICKERS) -> None:
    """The actual scheduled-job entrypoint: resolves each tracked
    ticker to a CIK, pulls recent filings, upserts. No manual
    --cik/--issuer-id needed — this is what deploy.sh's edgar-puller
    Cloud Run job should call."""
    ticker_to_cik = load_ticker_to_cik_map()
    if not ticker_to_cik:
        return

    for ticker in tickers:
        cik = ticker_to_cik.get(ticker)
        if not cik:
            log.warning("No CIK found for ticker %s — skipping", ticker)
            continue

        with get_session() as db:
            issuer = (
                db.query(Issuer)
                .filter(Issuer.ticker == ticker, Issuer.exchange.in_(["NYSE", "NASDAQ", "US"]))
                .one_or_none()
            )
            if issuer is None:
                issuer = Issuer(name=ticker, ticker=ticker, exchange="US")
                db.add(issuer)
                db.flush()
            issuer_id = issuer.id

        filings = pull_company_filings(cik)
        log.info("%s (CIK %s): %d recent filings", ticker, cik, len(filings))
        if filings:
            store_filings(issuer_id, filings)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pull SEC EDGAR filings — for tracked tickers by default, or a single CIK.")
    parser.add_argument("--cik", type=int, default=None, help="Pull a single company by CIK instead of the tracked list")
    parser.add_argument("--issuer-id", type=int, default=None, help="Required when --cik is used")
    parser.add_argument("--tickers", type=str, nargs="*", default=TRACKED_US_TICKERS)
    args = parser.parse_args()

    if args.cik:
        if not args.issuer_id:
            parser.error("--issuer-id is required when --cik is provided")
        result = pull_company_filings(args.cik)
        log.info("Fetched %d filings for CIK %s", len(result), args.cik)
        if result:
            store_filings(args.issuer_id, result)
    else:
        run_for_tracked_tickers(args.tickers)
