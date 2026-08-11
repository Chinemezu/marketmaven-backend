# Marketmaven — Ingestion Pipeline

> This is the `/backend` folder of the MarketMaven monorepo — see the root `README.md` for the full repo structure and how this relates to `/frontend`. On Render, set this service's **Root Directory to `backend`** so build/start commands below run relative to this folder correctly.

## Layout

```
api/
  database.py     SQLAlchemy engine/session (Supabase Postgres)
  models.py       ORM models — issuers, prices_daily, indices_daily,
                   filings, fx_rates_daily, peer_mappings, insights
  schemas.py      Pydantic response models
  main.py         FastAPI app — /issuers, /prices, /indices, /filings, /insights, /benchmark
ingestion/
  ngx_scraper.py            NGX Daily Official List (PDF, date-driven URL)
  edgar_client.py           SEC EDGAR filings (official, free API)
  us_market_puller.py       yfinance — US indices + peer-ticker prices
  insights_aggregator.py    RSS/Atom news via feedparser, relevance-scored
  populate_peer_mappings.py Curated NGX<->US sector peer seed data — manual/on-demand, not scheduled
  test_ngx_parser.py        Regression tests for the NGX parser + prior-close logic
  fixtures/                 Real bulletin text used to calibrate/test the NGX parser
Dockerfile        One image, all five ingestion jobs
deploy.sh         Cloud Run jobs + Cloud Scheduler triggers
requirements.txt
```

## Environment variables

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | all ingestion jobs, API | Supabase Postgres connection string (use the connection-pooler/Transaction-mode URI, not the direct connection — see the implementation runbook for why). |
| `SEC_USER_AGENT` | `edgar_client.py` | SEC requires a descriptive User-Agent with a real contact email. Set this before running against real EDGAR endpoints — the default in the code is a placeholder and should not ship as-is. |
| `JWT_SECRET` | API (auth) | **Critical** — the default in `auth.py` is an insecure placeholder. Generate a real one: `python -c "import secrets; print(secrets.token_hex(32))"`. Anyone with this value can forge valid login tokens. |
| `RESEND_API_KEY` | API (`mailer.py`) | From resend.com. Without this set, the API still runs — email sends are logged and skipped rather than crashing registration/reset flows, which is fine for local dev but means verification/reset emails silently won't go out until this is set. |
| `EMAIL_FROM` | API (`mailer.py`) | Defaults to a Resend sandbox address — replace with a verified sending domain before real users register. |
| `FRONTEND_BASE_URL` | API (`mailer.py`) | Used to build the verification/reset links inside emails — must point at the real deployed frontend, not localhost, once live. |
| `FRONTEND_ORIGINS` | API (CORS) | Comma-separated list of allowed frontend origins. |

## Local run (before touching GCP)

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
export SEC_USER_AGENT="Marketmaven contact@yourdomain.com"
export JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
# RESEND_API_KEY optional locally — email sends just log and skip without it

python ingestion/ngx_scraper.py --date 2026-07-22
python ingestion/us_market_puller.py
python ingestion/edgar_client.py                              # tracked-ticker loop (scheduled default)
python ingestion/edgar_client.py --cik 320193 --issuer-id 1   # or a one-off single company by CIK
python ingestion/insights_aggregator.py
python ingestion/populate_peer_mappings.py   # re-run after editing SECTOR_PEERS

uvicorn api.main:app --reload --app-dir api   # http://localhost:8000/docs
```

## Deploying

```bash
chmod +x deploy.sh
./deploy.sh
```

Edit `PROJECT_ID`, `REGION`, and `SCHEDULER_SA` at the top of the script first.

## Known gaps — next things to close, not blockers to starting

1. **NGX PDF field scope is intentionally narrow.** The parser is calibrated against a real bulletin (see `ingestion/fixtures/` and `ingestion/test_ngx_parser.py`) and reliably extracts symbol, name, trade date, volume, and a best-effort `last_price`. `prev_close`/`change_pct` ARE now real — computed from our own stored price history on each upsert (see `upsert_rows` in `ngx_scraper.py`), not parsed from the bulletin. Still NOT extracted: Official Open, 52wk High/Low, dividends, EPS, P.E. — those columns get omitted (not zero-filled) on a per-row basis in the source, which shifts everything left in flattened text and makes positional mapping genuinely ambiguous without pdfplumber's word x-coordinates.
2. **`TRACKED_US_TICKERS` is a starting list, not a real peer set.** `edgar_client.py` and `us_market_puller.py` both default to the same 9 tickers — matches the tickers seeded in `populate_peer_mappings.py`, but worth widening as coverage grows.
3. ~~`peer_mappings` is an empty table.~~ **Resolved** — `populate_peer_mappings.py` seeds curated NGX↔US sector pairs (banking, telecom, consumer goods, oil & gas) with confidence scores reflecting how comparable each pairing actually is. Run it manually after any edit to `SECTOR_PEERS`; it's deployed but not on a recurring schedule (see `deploy.sh`).
4. **FX rates aren't wired in.** Flagged in the pipeline scope doc — still need a NGN/USD source for the dual-currency views.
5. **`FEEDS` URLs in `insights_aggregator.py` are unverified.** They follow known patterns (WordPress `/feed/`, standard Reuters/MarketWatch RSS paths) but haven't been fetched and confirmed live — check each before trusting the aggregator's output. A dead URL fails silently (zero entries), not loudly.
6. **`TRACKED_KEYWORDS` is a first-pass vocabulary.** It'll over-match on generic terms like "financials" and under-match on stories that are relevant but don't use the exact tracked phrases — tune it against real feed output once you can see what's coming through.
7. **NGX ASI index-level value isn't captured.** The scraper is equities-only — the index summary itself needs a separate extraction target. This is what blocks the dashboard's ASI card/sparkline from being real.
8. **`Issuer.sector` is NULL for every NGX issuer.** The parser currently discards sector/sub-sector labels as boilerplate while isolating data rows — capturing them (rather than stripping them) would unlock a real sector heatmap.

## Testing the NGX parser

```bash
python ingestion/test_ngx_parser.py
```

Fixtures in `ingestion/fixtures/` are real text from the 20-04-2026 NGX bulletin, kept specifically because they cover the cases that broke earlier versions of the parser: wrapped company names spanning 1–3 lines, numbers concatenated with no separator, a date glued directly onto the preceding price, and duplicate ticker rows. Re-run this test after any change to the parsing logic in `ngx_scraper.py` — and add a new fixture whenever a real bulletin surfaces a case these two don't cover.
