"""
Automated daily market-wrap report generator.

Queries the real data already sitting in Postgres — index levels, tracked
issuer prices, and recent high-relevance Insights headlines — hands it to
Gemini as structured facts, and asks it to synthesize a report from
*only* those facts. Lands as a draft (status="draft", author "MarketMaven")
for a human to review and publish; never auto-publishes.

Why this is scoped the way it is: the NGX scraper is deliberately not
run against production yet (see ngx_scraper.py's docstring — NGX changed
their bulletin layout and the parser needs real recalibration first), so
there is currently zero NGX price/index data in prices_daily/indices_daily.
The facts gathered here reflect that honestly — real US index/price
numbers, real recent headlines (including NGX-related ones, attributed to
their actual source) — and the prompt explicitly forbids inventing NGX
price figures we don't have. Once NGX ingestion is live, extend
gather_facts() to include it; don't just ask the model to "cover NGX too"
without the data to back it.

Run as a one-off (e.g. from a scheduled GitHub Actions job):
    python generate_daily_report.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session                              # noqa: E402
from models import Issuer, IndexDaily, PriceDaily, Insight, Report, User, PeerMapping  # noqa: E402
import mailer                                                   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_daily_report")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-flash-latest"  # stable alias, not a pinned version — see mailer.py's RESEND_API_KEY pattern for why unset-key handling matters here too
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

AUTHOR_EMAIL = "MarketMaven@marketmaven.app"  # email-prefix-as-display-name convention (see schemas.py's UserOut.name) makes this resolve to author_name="MarketMaven"
ADMIN_NOTIFICATION_EMAIL = os.environ.get("ADMIN_NOTIFICATION_EMAIL")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")

INDEX_LABELS = {"SPX": "S&P 500", "IXIC": "Nasdaq Composite", "DJI": "Dow Jones Industrial Average"}


@dataclass
class Facts:
    """Everything the model is allowed to talk about — nothing else.
    Serialized straight into the prompt as JSON so there's no lossy
    English paraphrasing between "what we know" and "what the model sees."
    """
    report_date: str
    indices: list[dict] = field(default_factory=list)
    us_price_moves: list[dict] = field(default_factory=list)
    peer_pairs: list[dict] = field(default_factory=list)
    recent_headlines: list[dict] = field(default_factory=list)

    def has_enough_to_write_about(self) -> bool:
        # Refuse to generate off essentially nothing — an empty/near-empty
        # report is worse than no report, and shouldn't reach a draft.
        return len(self.indices) >= 1 or len(self.us_price_moves) >= 3


def gather_facts(db) -> Facts:
    today = date.today()
    facts = Facts(report_date=today.isoformat())

    for code, label in INDEX_LABELS.items():
        rows = (
            db.query(IndexDaily)
            .filter(IndexDaily.index_code == code)
            .order_by(IndexDaily.trade_date.desc())
            .limit(2)
            .all()
        )
        if not rows:
            continue
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        entry = {
            "code": code, "label": label, "trade_date": latest.trade_date.isoformat(),
            "value": float(latest.value),
        }
        if prev:
            entry["prev_value"] = float(prev.value)
            entry["change_pct"] = round((float(latest.value) - float(prev.value)) / float(prev.value) * 100, 2)
        facts.indices.append(entry)

    # Most recent trade_date per US issuer, with the change_pct already
    # computed at ingestion time (see us_market_puller.py) — not
    # recomputed here, so this can't drift from what the API itself serves.
    latest_prices = (
        db.query(PriceDaily, Issuer)
        .join(Issuer, PriceDaily.issuer_id == Issuer.id)
        .filter(Issuer.exchange.in_(["NYSE", "NASDAQ", "US"]))
        .order_by(PriceDaily.issuer_id, PriceDaily.trade_date.desc())
        .all()
    )
    seen_issuers = set()
    for price, issuer in latest_prices:
        if issuer.id in seen_issuers:
            continue
        seen_issuers.add(issuer.id)
        facts.us_price_moves.append({
            "ticker": issuer.ticker, "name": issuer.name, "trade_date": price.trade_date.isoformat(),
            "close": float(price.close),
            "change_pct": float(price.change_pct) if price.change_pct is not None else None,
        })

    facts.peer_pairs = _peer_pairs(db)

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    top_insights = (
        db.query(Insight)
        .filter(Insight.published_date >= since)
        .order_by(Insight.relevance_score.desc())
        .limit(8)
        .all()
    )
    facts.recent_headlines = [
        {"title": i.title, "source": i.source, "vertical": i.vertical}
        for i in top_insights
    ]

    return facts


def _peer_pairs(db) -> list[dict]:
    from sqlalchemy.orm import aliased
    NgxIssuer = aliased(Issuer)
    UsIssuer = aliased(Issuer)
    rows = (
        db.query(PeerMapping, NgxIssuer, UsIssuer)
        .join(NgxIssuer, PeerMapping.ngx_issuer_id == NgxIssuer.id)
        .join(UsIssuer, PeerMapping.us_peer_issuer_id == UsIssuer.id)
        .all()
    )
    return [
        {
            "ngx_ticker": ngx.ticker, "ngx_name": ngx.name,
            "us_ticker": us.ticker, "us_name": us.name,
            "sector": mapping.sector,
        }
        for mapping, ngx, us in rows
    ]


PROMPT_TEMPLATE = """You are writing a short daily market-wrap report for MarketMaven, a financial intelligence platform. Today's date is {report_date}.

CRITICAL RULES:
1. You may ONLY state numbers, prices, percentages, or facts that appear explicitly in the FACTS JSON below. Never invent, estimate, or infer a number that isn't there. For readability, round percentages to 1 decimal place and price levels to 2 decimal places when writing prose — this is display rounding of a real given value, not a new number.
2. There is currently NO real price or index data for the Nigerian Exchange (NGX) in our system. If any of the recent_headlines mention NGX or Nigerian markets, you may reference that news (attributed to its real source, e.g. "according to Nairametrics") but you must NEVER state a specific NGX price, index level, or percentage move as if MarketMaven measured it — we didn't.
3. If facts are sparse, write a short, honest report reflecting that. Do not pad with generic market commentary not tied to the given facts.
4. peer_pairs describes which NGX ticker is tracked against which US ticker as a sector comparison — you may mention the pairing itself as a fact, but only cite the US side's actual numbers (never the NGX side's, per rule 2).

FACTS:
{facts_json}

Return ONLY a JSON object with exactly these fields:
- "title": a specific, factual headline (not generic — reference an actual number or event from the facts)
- "summary": one or two sentences summarizing the report
- "body": the full report, 3-5 short paragraphs separated by \\n\\n, written in plain prose (no markdown headers)
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "body": {"type": "STRING"},
    },
    "required": ["title", "summary", "body"],
}


def call_gemini(facts: Facts) -> dict | None:
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — skipping report generation")
        return None

    import json
    prompt = PROMPT_TEMPLATE.format(report_date=facts.report_date, facts_json=json.dumps(facts.__dict__, indent=2))

    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": RESPONSE_SCHEMA,
                    "temperature": 0.3,  # low — this is factual synthesis, not creative writing
                },
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        log.error("Gemini request failed: %s", exc)
        return None

    if resp.status_code >= 400:
        log.error("Gemini API error %s: %s", resp.status_code, resp.text)
        return None

    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, ValueError) as exc:
        log.error("Couldn't parse Gemini response: %s — raw: %s", exc, resp.text[:500])
        return None


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:240]


def _get_or_create_author(db) -> User:
    author = db.query(User).filter(User.email == AUTHOR_EMAIL).one_or_none()
    if author is None:
        # No password is ever set for this account — it can't log in, it
        # exists only to be a foreign key with the right display name.
        author = User(email=AUTHOR_EMAIL, password_hash="", is_verified=True, is_admin=False)
        db.add(author)
        db.flush()
    return author


def notify_admin(report_title: str, report_id: int) -> None:
    if not ADMIN_NOTIFICATION_EMAIL:
        log.info("ADMIN_NOTIFICATION_EMAIL not set — skipping review notification")
        return
    review_url = f"{FRONTEND_BASE_URL}/admin-reports"
    html = f"""
    <p>A new automated draft report is ready for review:</p>
    <p><strong>{report_title}</strong></p>
    <p><a href="{review_url}">Review and publish in the admin panel</a></p>
    """
    mailer.send_email(ADMIN_NOTIFICATION_EMAIL, f"Draft ready for review: {report_title}", html)


def run() -> None:
    with get_session() as db:
        facts = gather_facts(db)
        if not facts.has_enough_to_write_about():
            log.info("Not enough real data to generate a meaningful report today — skipping.")
            return

        generated = call_gemini(facts)
        if generated is None:
            return

        author = _get_or_create_author(db)
        slug = _slugify(generated["title"])
        if db.query(Report).filter(Report.slug == slug).one_or_none():
            slug = f"{slug}-{date.today().isoformat()}"

        report = Report(
            slug=slug, title=generated["title"], author_id=author.id, vertical="finance",
            summary=generated["summary"], body=generated["body"], status="draft",
        )
        db.add(report)
        db.flush()
        report_id, report_title = report.id, report.title

    log.info("Created draft report #%d: %s", report_id, report_title)
    notify_admin(report_title, report_id)


if __name__ == "__main__":
    run()
