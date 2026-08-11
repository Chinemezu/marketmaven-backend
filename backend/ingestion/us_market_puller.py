"""
US benchmarking data via yfinance.

Free, unofficial, no published SLA — acceptable for MVP since it's
only the US side of the comparison (EDGAR carries the official-source
weight for anything filing-related). Revisit if reliability becomes a
problem once there's real usage on this.

Two jobs in one file, run separately so one failing doesn't block the
other:
- pull_indices(): S&P 500, Nasdaq Composite, Dow — feeds IndexDaily,
  which is what the /benchmark API endpoint reads from.
- pull_us_peer_prices(): daily OHLCV for whatever US tickers are
  already registered as Issuers (exchange in NYSE/NASDAQ) — feeds
  PriceDaily, same table the NGX scraper writes to.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session          # noqa: E402
from models import Issuer, PriceDaily, IndexDaily  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("us_market_puller")

# Maps our internal index_code to the yfinance ticker that carries it.
INDEX_TICKERS = {
    "SPX": "^GSPC",
    "IXIC": "^IXIC",
    "DJI": "^DJI",
}


def pull_indices(target_date: date | None = None) -> None:
    target_date = target_date or date.today()
    # yfinance's `period="5d"` with a trailing slice is more reliable
    # than requesting a single day directly — single-day requests can
    # come back empty around holidays/timezone edges.
    start = target_date - timedelta(days=7)
    end = target_date + timedelta(days=1)

    with get_session() as db:
        for code, ticker in INDEX_TICKERS.items():
            hist = yf.Ticker(ticker).history(start=start, end=end)
            if hist.empty:
                log.warning("No yfinance data for %s (%s) in range %s–%s", code, ticker, start, end)
                continue

            for idx_date, row in hist.iterrows():
                trade_date = idx_date.date()
                existing = (
                    db.query(IndexDaily)
                    .filter(IndexDaily.index_code == code, IndexDaily.trade_date == trade_date)
                    .one_or_none()
                )
                value = float(row["Close"])
                if existing:
                    existing.value = value
                else:
                    db.add(IndexDaily(index_code=code, trade_date=trade_date, value=value))

            log.info("Upserted %s (%s) through %s", code, ticker, target_date)


def pull_us_peer_prices(target_date: date | None = None) -> None:
    target_date = target_date or date.today()
    start = target_date - timedelta(days=7)
    end = target_date + timedelta(days=1)

    with get_session() as db:
        us_issuers = (
            db.query(Issuer)
            .filter(Issuer.exchange.in_(["NYSE", "NASDAQ"]))
            .all()
        )
        if not us_issuers:
            log.info("No US issuers registered yet — nothing to pull. "
                      "Populate peer_mappings / issuers first.")
            return

        for issuer in us_issuers:
            hist = yf.Ticker(issuer.ticker).history(start=start, end=end)
            if hist.empty:
                log.warning("No yfinance data for %s", issuer.ticker)
                continue

            prev_close = None
            for idx_date, row in hist.iterrows():
                trade_date = idx_date.date()
                close = float(row["Close"])
                change_pct = ((close - prev_close) / prev_close * 100) if prev_close else None

                existing = (
                    db.query(PriceDaily)
                    .filter(PriceDaily.issuer_id == issuer.id, PriceDaily.trade_date == trade_date)
                    .one_or_none()
                )
                fields = dict(
                    open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]),
                    close=close, prev_close=prev_close, change_pct=change_pct,
                    volume=int(row["Volume"]),
                )
                if existing:
                    for k, v in fields.items():
                        setattr(existing, k, v)
                else:
                    db.add(PriceDaily(issuer_id=issuer.id, trade_date=trade_date, currency="USD", **fields))

                prev_close = close

            log.info("Upserted prices for %s through %s", issuer.ticker, target_date)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pull US index and peer-ticker prices via yfinance.")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--skip-indices", action="store_true")
    parser.add_argument("--skip-peers", action="store_true")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    if not args.skip_indices:
        pull_indices(target)
    if not args.skip_peers:
        pull_us_peer_prices(target)
