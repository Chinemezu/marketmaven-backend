"""
Core data model. Mirrors the tables laid out in
marketmaven-mvp-pipeline.md — keep that doc and this file in sync
when either changes.
"""
from datetime import date, datetime

from sqlalchemy import (
    String, Integer, Numeric, Date, DateTime, ForeignKey,
    UniqueConstraint, Text, Boolean, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Issuer(Base):
    __tablename__ = "issuers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)  # NGX / NYSE / NASDAQ
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prices: Mapped[list["PriceDaily"]] = relationship(back_populates="issuer")
    filings: Mapped[list["Filing"]] = relationship(back_populates="issuer")

    __table_args__ = (UniqueConstraint("ticker", "exchange", name="uq_issuer_ticker_exchange"),)


class PriceDaily(Base):
    __tablename__ = "prices_daily"

    id: Mapped[int] = mapped_column(primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    prev_close: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")

    issuer: Mapped["Issuer"] = relationship(back_populates="prices")

    __table_args__ = (UniqueConstraint("issuer_id", "trade_date", name="uq_price_issuer_date"),)


class IndexDaily(Base):
    __tablename__ = "indices_daily"

    id: Mapped[int] = mapped_column(primary_key=True)
    index_code: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # NGX_ASI, SPX, IXIC...
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    value: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    market_cap: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)

    __table_args__ = (UniqueConstraint("index_code", "trade_date", name="uq_index_code_date"),)


class Filing(Base):
    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(primary_key=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # NGX / SEC_NG / EDGAR
    filing_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filing_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    issuer: Mapped["Issuer"] = relationship(back_populates="filings")


class FxRateDaily(Base):
    __tablename__ = "fx_rates_daily"

    id: Mapped[int] = mapped_column(primary_key=True)
    pair: Mapped[str] = mapped_column(String(10), nullable=False)  # e.g. NGNUSD
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    rate: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    __table_args__ = (UniqueConstraint("pair", "trade_date", name="uq_fx_pair_date"),)


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)  # feed name, e.g. "Nairametrics"
    vertical: Mapped[str] = mapped_column(String(30), nullable=False, default="finance", index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    published_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only populated when the source feed actually provides one (media:content,
    # media:thumbnail, an enclosure, or an <img> embedded in the summary/content
    # HTML) — see insights_aggregator.py's extract_image_url(). Left null rather
    # than filled with a stock photo when the feed has nothing; the frontend
    # falls back to a generic placeholder in that case.
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_keywords: Mapped[str | None] = mapped_column(String(300), nullable=True)  # comma-separated
    # Hybrid curation: relevance_score drives the algorithmic base ranking;
    # featured is a manual override for pinning specific items above it,
    # independent of score. featured_order lets multiple pinned items be
    # sequenced deliberately (lower = higher up) rather than by score.
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    featured_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_token_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    watchlist_items: Mapped[list["WatchlistItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    saved_articles: Mapped[list["SavedArticle"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class WatchlistItem(Base):
    """Plain watchlist per the confirmed decision — followed tickers only,
    no quantity/cost-basis fields, no P&L calculation."""
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="watchlist_items")
    issuer: Mapped["Issuer"] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "issuer_id", name="uq_watchlist_user_issuer"),)


class SavedArticle(Base):
    __tablename__ = "saved_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    insight_id: Mapped[int] = mapped_column(ForeignKey("insights.id"), nullable=False)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="saved_articles")
    insight: Mapped["Insight"] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "insight_id", name="uq_saved_user_insight"),)


class NewsletterSignup(Base):
    __tablename__ = "newsletter_signups"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    signed_up_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    """First-party editorial content — the actual gap flagged earlier as
    'Opinion'/'Briefs' (deferred, since no authoring pipeline existed).
    Distinct from Insight in a real way, not just naming: Insight is
    aggregated (external source, we never wrote it), Report is authored
    (real staff, real body content, drafted and published through the
    admin panel) — hence author_id pointing at User rather than a
    free-text source string."""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(250), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    vertical: Mapped[str] = mapped_column(String(30), nullable=False, default="finance", index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # markdown or HTML — renderer is a frontend concern
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")  # draft | published
    is_newsletter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    newsletter_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    featured_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    author: Mapped["User"] = relationship()


class PeerMapping(Base):
    __tablename__ = "peer_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    ngx_issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False)
    us_peer_issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mapping_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)  # 0.00-1.00

    __table_args__ = (
        UniqueConstraint("ngx_issuer_id", "us_peer_issuer_id", name="uq_peer_mapping_pair"),
    )
