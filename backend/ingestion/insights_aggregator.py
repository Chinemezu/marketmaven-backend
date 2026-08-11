"""
Insights aggregator — pulls capital markets news via RSS/Atom
(feedparser), scores each article for relevance to Marketmaven's
tracked terms, and stores anything above threshold.

feedparser gives a consistent .entries structure regardless of feed
format, and supports conditional GETs (etag/modified) so repeat runs
don't re-download feeds that haven't changed — worth keeping even at
MVP scale since it's close to free.

VERIFY THE FEED URLS BELOW before relying on this. They're the
standard WordPress /feed/ pattern for the Nigerian sites and known
Reuters/MarketWatch RSS paths, but I haven't fetched and confirmed
each one is live and correctly formatted — a broken URL just yields
zero entries for that source rather than an error, so a silent
mismatch is the actual failure mode to watch for.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import mktime, sleep

import feedparser

sys.path.append(str(Path(__file__).resolve().parent.parent / "api"))
from database import get_session   # noqa: E402
from models import Insight          # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("insights_aggregator")

# Feeds grouped by vertical: {vertical: {name: feed_url}}. VERIFY EACH
# before production use — see module docstring.
#
# Only "finance" is populated for launch — this is a deliberate scope
# decision (see /areas/marketmaven.md): the portal architecture
# supports multiple verticals from day one so nothing needs rewriting
# later, but sourcing and vetting real entertainment/sports feeds is
# separate work, done as a fast-follow after the finance-only launch.
FEEDS: dict[str, dict[str, str]] = {
    "finance": {
        "Nairametrics": "https://nairametrics.com/feed/",
        "BusinessDay NG": "https://businessday.ng/feed/",
        "Proshare Nigeria": "https://www.proshareng.com/rss/news.xml",
        "Reuters Markets": "https://www.reutersagency.com/feed/?best-topics=markets",
        "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    },
    "crypto": {
        "Cointelegraph": "https://cointelegraph.com/rss",
        "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "CryptoSlate": "https://cryptoslate.com/feed/",
    },
    "forex": {
        "ForexLive": "https://investinglive.com/rss/",
        "BabyPips": "https://www.babypips.com/feed.rss",
    },
    "bonds": {
        "Barron's Bonds": "https://www.barrons.com/topics/bonds/rss",
    },
    "etfs": {
        "ETF Trends": "https://www.etftrends.com/feed/",
    },
    "commodities": {
        "OilPrice.com": "https://oilprice.com/rss/main",
    },
    "technology": {
        # Confirmed via two independent sources — CNBC does not appear
        # to have separate official RSS feeds for AI/Cybersecurity
        # specifically; both fold into this general Technology feed.
        "CNBC Technology": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    },
    "real_estate": {
        "HousingWire": "https://www.housingwire.com/feed/",
    },
    "energy": {
        "OilPrice.com": "https://oilprice.com/rss/main",
    },
    "entertainment": {},  # still fast-follow — no feeds sourced/vetted yet
    "sports": {},          # still fast-follow — no feeds sourced/vetted yet
}

# Relevance vocabulary is currently finance-only. Each vertical should
# get its own keyword set once real feeds are added — scoring
# entertainment articles against finance terms would under-match
# almost everything and defeat the point of relevance filtering.
TRACKED_KEYWORDS: dict[str, set[str]] = {
    "finance": {
        # Nigeria capital markets
        "ngx", "nigerian exchange", "sec nigeria", "naira", "cbn", "nairametrics",
        "nigerian stock", "capital market", "asi",
        # US benchmarking
        "s&p 500", "nasdaq", "nyse", "sec filing", "wall street", "dow jones",
        "treasury yield", "fed rate", "federal reserve",
        # Tracked sectors/tickers (keep in sync with TRACKED_US_TICKERS elsewhere)
        "jpmorgan", "bank of america", "wells fargo", "exxon", "chevron",
        "banking sector", "financials",
    },
    # These vocabularies are intentionally light — each of these verticals
    # pulls from a small number of already-topic-dedicated feeds (a crypto
    # publication's RSS is about crypto by definition), so the keyword
    # list exists mainly to catch clearly off-topic posts (a crypto site's
    # unrelated sponsored content, etc.), not to do heavy-lifting
    # classification the way the finance vocabulary does across
    # general-purpose business feeds.
    "crypto": {"bitcoin", "ethereum", "crypto", "blockchain", "defi", "altcoin", "token", "web3"},
    "forex": {"forex", "currency", "fx", "exchange rate", "dollar", "euro", "pip", "central bank"},
    "bonds": {"bond", "yield", "treasury", "fixed income", "rate hike", "coupon"},
    "etfs": {"etf", "fund", "index fund", "exchange-traded"},
    "commodities": {"oil", "gold", "commodity", "opec", "crude", "gas", "mining"},
    "technology": {"ai", "artificial intelligence", "cybersecurity", "tech", "software", "startup", "automation"},
    "real_estate": {"real estate", "housing", "mortgage", "property", "reit"},
    "energy": {"energy", "oil", "gas", "renewable", "solar", "opec", "power grid"},
}

REQUEST_DELAY_SECONDS = 1.0  # be polite between feeds — no rush for a periodic job
MIN_RELEVANCE_TO_STORE = 1    # articles scoring 0 aren't stored at all


def score_relevance(title: str, summary: str, vertical: str) -> tuple[int, list[str]]:
    text = f"{title} {summary}".lower()
    keywords = TRACKED_KEYWORDS.get(vertical, set())
    matched = [kw for kw in keywords if kw in text]
    return len(matched), matched


def _parse_published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)


def _clean_summary(raw: str | None) -> str | None:
    if not raw:
        return None
    # feed summaries often carry embedded HTML — strip tags, don't try to render them
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1000]  # keep it a summary, not the full article body


def fetch_and_score(source_name: str, feed_url: str, vertical: str) -> list[dict]:
    parsed = feedparser.parse(feed_url)

    if parsed.bozo:
        # bozo=True means the feed didn't parse cleanly — could be a dead
        # URL, a redirect, or malformed XML. Log it, don't crash the run.
        log.warning("%s: feed did not parse cleanly (%s)", source_name, parsed.get("bozo_exception"))

    results = []
    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        url = entry.get("link", "").strip()
        if not title or not url:
            continue

        summary = _clean_summary(entry.get("summary"))
        score, matched = score_relevance(title, summary or "", vertical)
        if score < MIN_RELEVANCE_TO_STORE:
            continue

        results.append({
            "source": source_name,
            "vertical": vertical,
            "title": title,
            "url": url,
            "published_date": _parse_published(entry),
            "summary": summary,
            "relevance_score": score,
            "matched_keywords": ", ".join(matched),
        })

    log.info("%s [%s]: %d entries fetched, %d met relevance threshold", source_name, vertical, len(parsed.entries), len(results))
    return results


def upsert_insights(items: list[dict]) -> int:
    inserted = 0
    with get_session() as db:
        for item in items:
            exists = db.query(Insight).filter(Insight.url == item["url"]).one_or_none()
            if exists:
                # relevance vocabulary can change over time — keep the
                # score current even for articles we've already stored
                exists.relevance_score = item["relevance_score"]
                exists.matched_keywords = item["matched_keywords"]
                continue
            db.add(Insight(**item))
            inserted += 1
    return inserted


def run(feeds_by_vertical: dict[str, dict[str, str]] = FEEDS) -> None:
    all_items = []
    for vertical, feeds in feeds_by_vertical.items():
        if not feeds:
            log.info("Skipping vertical '%s' — no feeds sourced yet", vertical)
            continue
        for name, url in feeds.items():
            all_items.extend(fetch_and_score(name, url, vertical))
            sleep(REQUEST_DELAY_SECONDS)

    inserted = upsert_insights(all_items)
    log.info("Inserted %d new insight records (of %d relevant entries seen)", inserted, len(all_items))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pull and score capital markets news via RSS.")
    parser.add_argument("--min-score", type=int, default=None, help="Override MIN_RELEVANCE_TO_STORE for this run")
    args = parser.parse_args()

    if args.min_score is not None:
        MIN_RELEVANCE_TO_STORE = args.min_score

    run()
