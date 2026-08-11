"""
NGX Daily Official List (Equities) ingestion job.

NGX publishes this at a predictable URL — no page-scraping needed to
discover it, just construct the link from the date:

    https://doclib.ngxgroup.com/DownloadsContent/Daily%20Official%20List%20-%20Equities%20for%20DD-MM-YYYY.pdf

Design choices, and why:
- Snapshot raw PDF bytes BEFORE parsing (to local disk or GCS). If the
  parser breaks tomorrow because NGX tweaks the layout, we re-run
  against today's snapshot instead of having lost the data.
- Parsing uses a token state machine calibrated against a real
  bulletin (see fixtures/ and test_ngx_parser.py), not line-based
  regex — the bulletin concatenates adjacent numbers with no
  separator in places, which a line regex can't reliably split.
  Extracted fields are intentionally narrow (symbol, name, trade
  date, volume, last_price) — see parse_rows_from_tokens' docstring
  for exactly what's reliable and what isn't yet.
- prev_close/change_pct are computed from OUR OWN stored price
  history on upsert, not parsed from the bulletin — see upsert_rows.
- No trading on weekends/public holidays: a 404 is expected and
  handled quietly, not treated as a failure.

Run as a Cloud Run job, e.g.:
    python ngx_scraper.py --date 2026-07-22
    python ngx_scraper.py                      # defaults to today
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from time import sleep
from urllib.parse import quote

import requests

# --- make sibling `api` package importable when run as a standalone script ---
sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session          # noqa: E402
from models import Issuer, PriceDaily      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ngx_scraper")

BASE_URL = "https://doclib.ngxgroup.com/DownloadsContent/"
SNAPSHOT_DIR = Path("./raw_snapshots/ngx")  # swap for a GCS upload in production
MAX_RETRIES = 3
BACKOFF_SECONDS = 5

# --- Calibrated against a real bulletin (20-04-2026). Structure confirmed: ---
# Board > Sector > Sub-sector > fixed column-header block > data rows.
# The header block below is copied verbatim from the real PDF and repeats
# byte-identical before every sub-sector — stripped by exact match, not regex.
HEADER_BLOCK = """Symbol Security Name
 Public 
Quotation 
Price (N) Official Open Official Close
Current 
Market Price
Ex - Business Done 52 wk Last 
ExDiv 
Date
Last 
ExSc 
Date
Dividends
Div Sc Price Date Qty High Low EPS P.E.
Date 
Paid InterimFinal"""

# Page-break boilerplate — repeats every ~page, strip before anything else.
BOILERPLATE_PATTERNS = [
    re.compile(r"Daily Official List \(Equities\) For \d{2}/\d{2}/\d{4}\n?"),
    re.compile(r"Print Date \d{2}/\d{2}/\d{4}\n?"),
    re.compile(r"Published by The Nigerian Stock Exchange © Page \d+ of\n\d+\n?"),
    re.compile(r"^(EQTY - Main Board|PREMIUM - Premium Board|REITCEF[^\n]*)\n?", re.MULTILINE),
]

# Recognizes a NGX ticker: all-caps/digits, no spaces, 1-15 chars — this is
# what lets the token FSM tell "start of a new row" apart from "another word
# in the company name" (both are all-caps, but a symbol is a single token
# immediately followed by more all-caps words, whereas we're mid-row once
# we've seen a date+qty pair).
SYMBOL_TOKEN = re.compile(r"^[A-Z][A-Z0-9]{0,14}$")
DATE_TOKEN = re.compile(r"^(\d{2}/\d{2}/\d{2})$")
GLUED_DATE_SUFFIX = re.compile(r"^(.*?)(\d{2}/\d{2}/\d{2})$")
QTY_TOKEN = re.compile(r"^[\d,]+$")
NUMERIC_TOKEN = re.compile(r"^[\d,]+\.\d{1,2}$")

# Sector/sub-sector title lines: standalone, letters/punctuation only, no
# digits. On their own this shape is indistinguishable from a wrapped
# company-name line (e.g. "FTN COCOA" / "PROCESSORS PLC" also has no
# digits) — the signal that actually disambiguates them is POSITION:
# title lines sit directly before the header block, wrapped name
# fragments don't. So this pattern is only ever applied anchored
# immediately before HEADER_BLOCK (see strip_boilerplate), never as a
# standalone per-line filter.
TITLE_LINE_BEFORE_HEADER = re.compile(
    r"(?:^[A-Za-z][A-Za-z /&\-,()]*\n){1,2}" + re.escape(HEADER_BLOCK),
    re.MULTILINE,
)


@dataclass
class ParsedRow:
    symbol: str
    name: str
    business_done_date: str | None   # dd/mm/yy as printed — reliably extracted (unambiguous date pattern)
    volume: int | None               # reliably extracted (integer token immediately after the date)
    last_price: float | None         # best-effort "Current Market Price" — see note below


def strip_boilerplate(raw_text: str) -> str:
    text = raw_text
    for pattern in BOILERPLATE_PATTERNS:
        text = pattern.sub("", text)
    # Removes each "<title line(s)>\n<HEADER_BLOCK>" unit as one piece —
    # anchoring the title match to its position right before the header
    # is what correctly distinguishes it from a wrapped company name
    # (see TITLE_LINE_BEFORE_HEADER docstring above).
    text = TITLE_LINE_BEFORE_HEADER.sub("", text)
    return text


def extract_data_tokens(cleaned_text: str) -> list[str]:
    """After strip_boilerplate has removed page furniture and every
    title+header unit, what's left should be exactly symbol/name/number
    tokens in document order — just tokenize it."""
    return cleaned_text.split()


def parse_rows_from_tokens(tokens: list[str]) -> list[ParsedRow]:
    """Token state machine — necessary because a data 'row' spans a
    variable number of physical lines (company names wrap), and because
    adjacent numeric columns sometimes concatenate with no separating
    space (e.g. '19.423.00'), which a line-based regex can't reliably
    split. Row boundary is unambiguous by a different signal: a new
    all-caps symbol-shaped token appearing right after we've already
    consumed a date+qty pair for the current row.

    Field scope is deliberately narrower than the full data model:
    - symbol, name, business_done_date, volume: extracted with high
      confidence — each has a distinctive, unambiguous token pattern.
    - last_price: best-effort. The column order in the header block is
      fixed (...Official Close | Current Market Price | Ex-Business
      Done Date...), so the numeric token immediately before the date
      token is structurally the Current Market Price *when* that
      column is populated for the row. But several rows in the real
      bulletin omit columns entirely when a value doesn't apply (rather
      than printing a blank/zero), which shifts everything left — so
      this mapping is a reasonable default, not a verified one. Treat
      `last_price` as an approximation until checked against real
      pdfplumber word positions (x-coordinates would resolve this
      properly; flattened text can't).
    - Official Open, 52wk High/Low, dividends, EPS, P.E. are NOT
      extracted here — column position for these is genuinely
      ambiguous from flattened text (see docstring above and the
      module-level TODO). Left for the word-position pass.
    """
    rows: list[ParsedRow] = []
    i = 0
    n = len(tokens)

    while i < n:
        token = tokens[i]
        if not SYMBOL_TOKEN.match(token):
            i += 1
            continue

        symbol = token
        i += 1
        name_parts = []

        # Company names never contain a decimal number — stop collecting
        # name tokens at the first one. (The previous version stopped at
        # the date instead, which let price tokens leak into the name.)
        while i < n and not NUMERIC_TOKEN.match(tokens[i]) and not DATE_TOKEN.match(tokens[i]):
            name_parts.append(tokens[i])
            i += 1
            if i >= n:
                break

        if i >= n:
            break

        # Everything from here to the date is price/sign data in some
        # order that varies row-to-row (columns get omitted rather than
        # zero-filled when not applicable — see docstring). Take the
        # rightmost numeric token as last_price, since the header's
        # fixed column order puts Current Market Price immediately
        # before the date regardless of which earlier columns were
        # populated for this row.
        price_tokens = []
        business_done_date = None

        while i < n:
            tok = tokens[i]

            if DATE_TOKEN.match(tok):
                business_done_date = tok
                i += 1
                break

            # Some rows have the date glued directly onto the preceding
            # number with no space (e.g. "1,000,000.0019/02/26") — the
            # AVAIF case. Recover the trailing date and treat whatever's
            # left as a further price token, rather than letting this
            # token block date detection entirely.
            glued = GLUED_DATE_SUFFIX.match(tok)
            if glued:
                prefix, embedded_date = glued.group(1), glued.group(2)
                if NUMERIC_TOKEN.match(prefix):
                    price_tokens.append(prefix)
                business_done_date = embedded_date
                i += 1
                break

            # Safety valve: if this token looks like the start of a new
            # row (symbol-shaped, followed by more uppercase words) and
            # we still haven't found a date, the current row's date is
            # missing/unrecoverable — stop here WITHOUT consuming this
            # token, so it starts the next row instead of being folded
            # into this one's price data. This is what fixes the
            # AVAIF/CNIF collision.
            if SYMBOL_TOKEN.match(tok) and _looks_like_row_start(tokens, i):
                break

            price_tokens.append(tok)
            i += 1

        numeric_price_tokens = [t for t in price_tokens if NUMERIC_TOKEN.match(t)]
        last_price = float(numeric_price_tokens[-1].replace(",", "")) if numeric_price_tokens else None

        volume = None
        if business_done_date is not None and i < n and QTY_TOKEN.match(tokens[i]):
            volume = int(tokens[i].replace(",", ""))
            i += 1

        # Skip everything else until the next symbol-shaped token —
        # this is where 52wk high/low, dividends, EPS, P.E. currently
        # get discarded (see docstring).
        while i < n and not (SYMBOL_TOKEN.match(tokens[i]) and _looks_like_row_start(tokens, i)):
            i += 1

        name = " ".join(name_parts).strip()
        if symbol and name:
            rows.append(ParsedRow(
                symbol=symbol, name=name,
                business_done_date=business_done_date,
                volume=volume, last_price=last_price,
            ))

    return rows


def _looks_like_row_start(tokens: list[str], idx: int) -> bool:
    """A symbol-shaped token starts a new row if it's followed by more
    all-caps word tokens before any date appears — distinguishes an
    actual ticker from a stray all-caps word inside preceding data
    (e.g. 'PLC' fragments don't reach here since they're consumed as
    part of the name in the row that owns them)."""
    return idx + 1 < len(tokens) and tokens[idx + 1].isupper()


def parse_pdf_text(raw_text: str) -> list[ParsedRow]:
    cleaned = strip_boilerplate(raw_text)
    tokens = extract_data_tokens(cleaned)
    rows = parse_rows_from_tokens(tokens)

    if not rows:
        log.warning(
            "Parsed 0 rows — likely a layout change or boilerplate patterns "
            "no longer matching. Raw PDF is snapshotted; recalibrate against it."
        )
    return rows


def build_url(trade_date: date) -> str:
    formatted = trade_date.strftime("%d-%m-%Y")
    filename = f"Daily Official List - Equities for {formatted}.pdf"
    return BASE_URL + quote(filename)


def fetch_pdf(trade_date: date) -> bytes | None:
    """Downloads the PDF with retry/backoff. Returns None on a clean
    404 (non-trading day) rather than raising, since that's expected
    behavior, not an error."""
    url = build_url(trade_date)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=20)
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            sleep(BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 404:
            log.info("No bulletin for %s (likely a non-trading day)", trade_date)
            return None

        log.warning("Unexpected status %s for %s (attempt %d/%d)", resp.status_code, url, attempt, MAX_RETRIES)
        sleep(BACKOFF_SECONDS * attempt)

    log.error("Giving up on %s after %d attempts", url, MAX_RETRIES)
    return None


def snapshot_raw(trade_date: date, content: bytes) -> Path:
    """Lands the raw PDF before any parsing happens. Swap this for a
    `gcs_bucket.blob(...).upload_from_string(content)` call once the
    GCP bucket exists — signature stays the same either way."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SNAPSHOT_DIR / f"{trade_date.isoformat()}.pdf"
    out_path.write_bytes(content)
    return out_path


def parse_pdf(pdf_path: Path) -> list[ParsedRow]:
    """Extracts text per page via pdfplumber, concatenates, and runs it
    through the calibrated boilerplate-stripping + token-FSM parser.
    Page-by-page extraction (not one extract_text() call on the whole
    doc) avoids pdfplumber inserting inconsistent spacing at page
    boundaries."""
    import pdfplumber  # imported here so the module still loads without it in tests

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    return parse_pdf_text(full_text)


def upsert_rows(trade_date: date, rows: list[ParsedRow]) -> None:
    """Stores what the calibrated parser can currently extract with
    confidence: symbol, name, volume, and last_price (mapped to
    `close` — see parse_rows_from_tokens docstring for why this is a
    best-effort mapping, not a verified one). `open`/`high`/`low` are
    intentionally left null rather than guessed.

    prev_close / change_pct: NOT parsed from the bulletin — the
    bulletin's own price columns are too ambiguous in flattened text
    to trust for this (see the module docstring's column-order note).
    Instead, this looks up the most recent PRIOR trading day already
    stored for the same issuer and computes change_pct from that. This
    means change_pct is unavailable for an issuer's first-ever ingested
    row (nothing to compare against) and self-heals from there — every
    day after, real change % becomes available."""
    with get_session() as db:
        for row in rows:
            if row.last_price is None:
                # Nothing worth storing without at least one price point —
                # PriceDaily.close is non-nullable by design.
                continue

            issuer = (
                db.query(Issuer)
                .filter(Issuer.ticker == row.symbol, Issuer.exchange == "NGX")
                .one_or_none()
            )
            if issuer is None:
                issuer = Issuer(name=row.name, ticker=row.symbol, exchange="NGX")
                db.add(issuer)
                db.flush()

            prior = (
                db.query(PriceDaily)
                .filter(PriceDaily.issuer_id == issuer.id, PriceDaily.trade_date < trade_date)
                .order_by(PriceDaily.trade_date.desc())
                .first()
            )
            prev_close = float(prior.close) if prior else None
            change_pct = (
                round((row.last_price - prev_close) / prev_close * 100, 4)
                if prev_close not in (None, 0)
                else None
            )

            existing = (
                db.query(PriceDaily)
                .filter(PriceDaily.issuer_id == issuer.id, PriceDaily.trade_date == trade_date)
                .one_or_none()
            )
            if existing:
                existing.close = row.last_price
                existing.volume = row.volume
                existing.prev_close = prev_close
                existing.change_pct = change_pct
            else:
                db.add(PriceDaily(
                    issuer_id=issuer.id,
                    trade_date=trade_date,
                    close=row.last_price,
                    volume=row.volume,
                    prev_close=prev_close,
                    change_pct=change_pct,
                    currency="NGN",
                ))
    log.info("Upserted %d rows for %s", len(rows), trade_date)


def run(trade_date: date) -> None:
    content = fetch_pdf(trade_date)
    if content is None:
        return  # non-trading day or unrecoverable fetch failure, already logged

    pdf_path = snapshot_raw(trade_date, content)
    rows = parse_pdf(pdf_path)
    if rows:
        upsert_rows(trade_date, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest the NGX Daily Official List for a given date.")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    run(target_date)
