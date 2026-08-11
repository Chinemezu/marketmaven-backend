# Single image for all three ingestion jobs — NGX, EDGAR, US market data.
# Which one runs is chosen by the CMD argument each Cloud Run job passes,
# not by building separate images. Keeps versioning/deploys in lockstep.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY ingestion/ ./ingestion/

# No CMD here on purpose — each Cloud Run job (see deploy.sh) sets its
# own `--command`/`--args` pointing at the script it needs, e.g.:
#   python ingestion/ngx_scraper.py
#   python ingestion/us_market_puller.py
#   python ingestion/edgar_client.py                       (tracked-ticker loop, scheduled default)
#   python ingestion/edgar_client.py --cik ... --issuer-id ...   (one-off single company)
#   python ingestion/insights_aggregator.py
#   python ingestion/populate_peer_mappings.py   (one-off/manual — see deploy.sh, no recurring schedule)
