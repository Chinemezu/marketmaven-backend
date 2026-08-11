#!/usr/bin/env bash
# Deploys the ingestion pipeline: one image, five Cloud Run jobs (four on
# a recurring Cloud Scheduler trigger, one — peer mapping seed — run
# manually on demand). Run this after `gcloud auth login` and
# `gcloud config set project <your-project>`.
#
# Prereqs (one-time, not scripted here since they're account-level):
#   - Artifact Registry repo created
#   - Secret Manager secret holding DATABASE_URL (the Supabase connection string)
#   - A service account with roles/run.invoker + roles/run.developer for
#     Cloud Scheduler to trigger the jobs
set -euo pipefail

PROJECT_ID="your-gcp-project"
REGION="us-central1"                      # pick whatever's closest to Supabase's region
REPO="marketmaven"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/ingestion:latest"
SCHEDULER_SA="marketmaven-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

echo "== Building and pushing the ingestion image =="
gcloud builds submit --tag "${IMAGE}" .

echo "== Creating/updating Cloud Run jobs =="

# --- NGX scraper: runs after market close (16:00 WAT = 15:00 UTC as of the
# April 2026 extended-hours change; using 16:00 UTC for a safety buffer) ---
gcloud run jobs deploy ngx-scraper \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command python \
  --args ingestion/ngx_scraper.py \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest \
  --max-retries 2 \
  --task-timeout 600

# --- US index + peer-ticker puller: runs after NYSE/NASDAQ close ---
gcloud run jobs deploy us-market-puller \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command python \
  --args ingestion/us_market_puller.py \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest \
  --max-retries 2 \
  --task-timeout 600

# --- EDGAR filings puller: resolves TRACKED_US_TICKERS to CIKs and pulls
# recent filings for each, no manual args needed ---
gcloud run jobs deploy edgar-puller \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command python \
  --args ingestion/edgar_client.py \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest \
  --set-env-vars SEC_USER_AGENT="Marketmaven contact@marketmaven.example" \
  --max-retries 2 \
  --task-timeout 600

# --- Insights aggregator: news isn't tied to market hours, so this runs
# every 6 hours rather than once daily ---
gcloud run jobs deploy insights-aggregator \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command python \
  --args ingestion/insights_aggregator.py \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest \
  --max-retries 2 \
  --task-timeout 300

# --- Peer mapping seed: curated NGX<->US sector pairs (see SECTOR_PEERS
# in the script). Deployed so it CAN be run on demand, but deliberately
# has no Cloud Scheduler trigger below — this only needs re-running when
# someone edits the seed list, not on a recurring cadence. Trigger it
# manually after any edit:
#   gcloud run jobs execute peer-mapping-seed --region "${REGION}"
gcloud run jobs deploy peer-mapping-seed \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command python \
  --args ingestion/populate_peer_mappings.py \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest \
  --max-retries 1 \
  --task-timeout 120

echo "== Deploying the API as a standing Cloud Run SERVICE (not a job) =="
# This is the piece that was missing before: the jobs above run once and
# exit, but the frontend needs a persistent, publicly-reachable API.
# FRONTEND_ORIGINS should be a comma-separated list of the real frontend
# URL(s) once known — CORS in main.py defaults to localhost otherwise.
gcloud run deploy marketmaven-api \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --command sh \
  --args -c,"cd api && uvicorn main:app --host 0.0.0.0 --port 8080" \
  --set-secrets DATABASE_URL=marketmaven-db-url:latest,JWT_SECRET=marketmaven-jwt-secret:latest,RESEND_API_KEY=marketmaven-resend-key:latest \
  --set-env-vars FRONTEND_ORIGINS="${FRONTEND_ORIGINS:-http://localhost:3000}",FRONTEND_BASE_URL="${FRONTEND_BASE_URL:-http://localhost:3000}",RATE_LIMIT="60/minute" \
  --port 8080 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 10

echo "== Creating Cloud Scheduler triggers =="

gcloud scheduler jobs create http ngx-scraper-daily \
  --location "${REGION}" \
  --schedule "0 16 * * 1-5" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/ngx-scraper:run" \
  --http-method POST \
  --oauth-service-account-email "${SCHEDULER_SA}" \
  --time-zone "UTC"

gcloud scheduler jobs create http us-market-puller-daily \
  --location "${REGION}" \
  --schedule "0 22 * * 1-5" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/us-market-puller:run" \
  --http-method POST \
  --oauth-service-account-email "${SCHEDULER_SA}" \
  --time-zone "UTC"

gcloud scheduler jobs create http edgar-puller-daily \
  --location "${REGION}" \
  --schedule "0 17 * * 1-5" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/edgar-puller:run" \
  --http-method POST \
  --oauth-service-account-email "${SCHEDULER_SA}" \
  --time-zone "UTC"

gcloud scheduler jobs create http insights-aggregator-6h \
  --location "${REGION}" \
  --schedule "0 */6 * * *" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/insights-aggregator:run" \
  --http-method POST \
  --oauth-service-account-email "${SCHEDULER_SA}" \
  --time-zone "UTC"

echo "Done. Trigger a manual run to test before trusting the schedule:"
echo "  gcloud run jobs execute ngx-scraper --region ${REGION}"
