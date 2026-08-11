# MarketMaven

A financial intelligence platform covering Nigerian and global capital markets — market data, aggregated news, and original Featured Reports.

## Repository structure

```
/backend     FastAPI app + data-ingestion pipeline, deployed to Render
.github/workflows/   Scheduled ingestion jobs (GitHub Actions) — see below
```

This repo holds the backend only. The frontend (React, built via a separate AI Studio-driven workflow) lives in its own repository and deploys independently — the two don't share a runtime.

## Deployment

| Piece | Where | Root directory setting |
|---|---|---|
| Backend API | Render (Web Service) | `backend` |
| Backend database | Render (PostgreSQL, free tier — **expires 30 days after creation**, plan accordingly) | — |
| Scheduled ingestion (NGX scraper, EDGAR, US market puller, insights aggregator) | GitHub Actions | runs from repo root, `working-directory: backend` per job |

Full deploy instructions: `backend/README.md` for the API, and see the launch guide for the end-to-end sequence.

## Why GitHub Actions workflows live at the true repo root

GitHub only detects workflow files in the top-level `.github/workflows/` directory — they can't live inside `/backend` even though that's where the scripts they run actually are. Each workflow uses `working-directory: backend` to bridge that gap. If you ever restructure the backend folder, these five YAML files need updating too — they're not automatically in sync with the folder layout.

## Environment variables

See `backend/README.md` for the full table (`DATABASE_URL`, `JWT_SECRET`, `RESEND_API_KEY`, etc.) — these are set per-deployment-target (Render dashboard, GitHub Actions secrets), never committed to this repo.
