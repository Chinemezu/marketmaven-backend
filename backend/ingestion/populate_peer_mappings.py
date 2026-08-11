"""
Peer-mapping seed data — NGX issuers paired with their closest US
sector peers, tracked in TRACKED_US_TICKERS in edgar_client.py and
us_market_puller.py.

Why curated instead of inferred: "closest US peer" isn't reducible to
a simple sector-code match — GTCO and JPMorgan are both "banks" but
operate at wildly different scale, regulatory regime, and business
mix. A human judgment call, made once and revisited as coverage grows,
is more honest than an automated match that implies false precision.
mapping_confidence reflects how comparable the pairing actually is,
not just whether the sectors nominally match — a large-cap NGX bank
next to a US regional bank is a closer match than next to JPMorgan's
global scale, and the confidence score should say so.

Extend SECTOR_PEERS as coverage grows. Each entry:
    (ngx_ticker, us_ticker, sector_label, confidence)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session   # noqa: E402
from models import Issuer, PeerMapping  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("populate_peer_mappings")

# (ngx_ticker, us_ticker, sector, confidence 0.00-1.00)
SECTOR_PEERS: list[tuple[str, str, str, float]] = [
    # Banking — NGX's largest, most liquid banks against a US
    # bulge-bracket (JPM) and two large regionals (BAC, WFC). Scale gap
    # is real (JPM's balance sheet dwarfs any NGX bank), so confidence
    # reflects "same business, different tier" rather than close parity.
    ("GTCO", "JPM", "Banking", 0.55),
    ("ZENITHBANK", "JPM", "Banking", 0.55),
    ("UBA", "BAC", "Banking", 0.50),
    ("ACCESSCORP", "BAC", "Banking", 0.50),
    ("FIRSTHOLDCO", "WFC", "Banking", 0.50),
    ("FIDELITYBK", "WFC", "Banking", 0.40),
    ("STERLINGNG", "WFC", "Banking", 0.35),
    ("WEMABANK", "WFC", "Banking", 0.35),

    # Telecommunications — MTN Nigeria and Airtel Africa are the two
    # NGX telecom majors; AT&T and Verizon are the closest US analogs
    # in terms of being large-scale mobile network operators.
    ("MTNN", "T", "Telecommunications", 0.55),
    ("MTNN", "VZ", "Telecommunications", 0.55),
    ("AIRTELAFRI", "T", "Telecommunications", 0.50),
    ("AIRTELAFRI", "VZ", "Telecommunications", 0.50),

    # Consumer goods — NGX food/beverage majors against Coca-Cola and
    # P&G as the closest large-cap US consumer staples comparables.
    ("NESTLE", "KO", "Consumer Goods", 0.45),
    ("NB", "KO", "Consumer Goods", 0.50),        # Nigerian Breweries
    ("GUINNESS", "KO", "Consumer Goods", 0.45),
    ("CADBURY", "KO", "Consumer Goods", 0.40),
    ("UNILEVER", "PG", "Consumer Goods", 0.55),  # both are local units of the same global parent category
    ("PZ", "PG", "Consumer Goods", 0.50),
    ("NASCON", "PG", "Consumer Goods", 0.35),
    ("DANGSUGAR", "PG", "Consumer Goods", 0.30),

    # Oil & Gas — NGX energy majors against ExxonMobil and Chevron.
    # Business-model fit is closer here than banking/consumer since
    # these are all integrated or E&P oil companies, but still a scale
    # mismatch worth reflecting in confidence.
    ("SEPLAT", "XOM", "Oil & Gas", 0.50),
    ("ARADEL", "XOM", "Oil & Gas", 0.45),
    ("OANDO", "CVX", "Oil & Gas", 0.40),
    ("CONOIL", "CVX", "Oil & Gas", 0.35),
    ("ETERNA", "CVX", "Oil & Gas", 0.30),
    ("TOTAL", "XOM", "Oil & Gas", 0.40),  # TotalEnergies Marketing Nigeria — downstream distributor, not the French parent
]


def _get_or_create_issuer(db, ticker: str, exchange: str) -> Issuer:
    """Peer mapping is seeded independently of whether the ingestion
    jobs have touched these tickers yet — creates a minimal stub if
    needed so this script can run in any order relative to the
    scrapers, and the real ingestion jobs fill in prices/filings for
    the same row afterward."""
    issuer = db.query(Issuer).filter(Issuer.ticker == ticker, Issuer.exchange == exchange).one_or_none()
    if issuer is None:
        issuer = Issuer(name=ticker, ticker=ticker, exchange=exchange)
        db.add(issuer)
        db.flush()
    return issuer


def run(seed: list[tuple[str, str, str, float]] = SECTOR_PEERS) -> None:
    created, skipped = 0, 0
    with get_session() as db:
        for ngx_ticker, us_ticker, sector, confidence in seed:
            ngx_issuer = _get_or_create_issuer(db, ngx_ticker, "NGX")
            # US issuers may already exist under "NYSE"/"NASDAQ"/"US"
            # depending on which ingestion job touched them first —
            # check the common set rather than assuming "US".
            us_issuer = (
                db.query(Issuer)
                .filter(Issuer.ticker == us_ticker, Issuer.exchange.in_(["NYSE", "NASDAQ", "US"]))
                .one_or_none()
            )
            if us_issuer is None:
                us_issuer = _get_or_create_issuer(db, us_ticker, "US")

            existing = (
                db.query(PeerMapping)
                .filter(
                    PeerMapping.ngx_issuer_id == ngx_issuer.id,
                    PeerMapping.us_peer_issuer_id == us_issuer.id,
                )
                .one_or_none()
            )
            if existing:
                existing.sector = sector
                existing.mapping_confidence = confidence
                skipped += 1
                continue

            db.add(PeerMapping(
                ngx_issuer_id=ngx_issuer.id,
                us_peer_issuer_id=us_issuer.id,
                sector=sector,
                mapping_confidence=confidence,
            ))
            created += 1

    log.info("Peer mappings: %d created, %d updated (of %d seed entries)", created, skipped, len(seed))


if __name__ == "__main__":
    run()
