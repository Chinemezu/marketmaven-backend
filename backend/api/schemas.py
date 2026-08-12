"""Pydantic response models — kept separate from the ORM models so the
API's public shape can evolve independently of the storage schema."""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, computed_field


class IssuerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ticker: str
    exchange: str
    sector: str | None = None


class PriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    change_pct: float | None = None
    volume: int | None = None
    currency: str


class IndexOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index_code: str
    trade_date: date
    value: float
    market_cap: float | None = None


class FilingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    filing_type: str | None = None
    filing_date: date
    url: str
    parsed_summary: str | None = None


class InsightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    vertical: str
    title: str
    url: str
    published_date: datetime | None = None
    summary: str | None = None
    image_url: str | None = None
    relevance_score: int
    featured: bool


class PeerMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ngx_ticker: str
    ngx_name: str
    us_ticker: str
    us_name: str
    sector: str | None = None
    mapping_confidence: float | None = None


class SourceRank(BaseModel):
    source: str
    article_count: int


# --- Reports (first-party editorial content) ---

class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    title: str
    author_name: str  # flattened from the author relationship, not the raw User object
    vertical: str
    summary: str
    cover_image_url: str | None = None
    status: str  # "draft" | "published" — always "published" on the public /reports list
    featured: bool
    featured_order: int | None = None
    published_at: datetime | None = None


class ReportDetailOut(ReportOut):
    body: str  # full content — only fetched on the detail view, not the list


class ReportCreateIn(BaseModel):
    title: str
    vertical: str = "finance"
    summary: str
    body: str
    cover_image_url: str | None = None
    status: str | None = None  # "draft" (default) | "published" — publishing sets published_at
    featured: bool | None = None
    featured_order: int | None = None


class ReportUpdateIn(BaseModel):
    title: str | None = None
    vertical: str | None = None
    summary: str | None = None
    body: str | None = None
    cover_image_url: str | None = None
    status: str | None = None  # "draft" | "published" — publishing sets published_at
    is_newsletter: bool | None = None
    featured: bool | None = None
    featured_order: int | None = None


class ReportSendNewsletterOut(BaseModel):
    report_id: int
    sent: int
    failed: int
    total: int


class EditorsPickOut(BaseModel):
    """Unified feed item — Editor's Picks mixes featured Insights
    (aggregated, pinned) and featured Reports (first-party) in one
    ranked list, distinguished by content_type so the frontend can
    render the right card variant (Report cards can show a 'by
    MarketMaven' byline and link to a full read; Insight cards behave
    as they already do everywhere else)."""
    content_type: str  # "insight" | "report"
    id: int
    title: str
    summary: str | None = None
    vertical: str
    source_or_author: str  # Insight.source, or the Report author's name
    url_or_slug: str        # Insight.url (external), or the Report's slug (internal route)
    featured_order: int | None = None
    published_date: datetime | None = None


# --- Auth ---

class UserRegisterIn(BaseModel):
    email: str
    password: str


class UserLoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    is_verified: bool
    is_admin: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def name(self) -> str:
        # There's no display-name field on User — same email-prefix
        # convention _report_out() already uses for author_name.
        return self.email.split("@")[0]


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class MessageOut(BaseModel):
    message: str


# --- Watchlist ---

class WatchlistItemOut(BaseModel):
    issuer_id: int
    ticker: str
    name: str
    exchange: str


class WatchlistAddIn(BaseModel):
    issuer_id: int


# --- Saved articles ---

class SavedArticleOut(BaseModel):
    """Reuses the same shape as InsightOut so the frontend can render
    saved articles with the exact same card component used everywhere
    else — no special-cased 'saved article card' needed."""
    id: int
    source: str
    vertical: str
    title: str
    url: str
    published_date: datetime | None = None
    summary: str | None = None
    image_url: str | None = None
    saved_at: datetime


# --- Admin ---

class AdminFeatureIn(BaseModel):
    featured: bool
    featured_order: int | None = None


class NewsletterBroadcastIn(BaseModel):
    subject: str
    html_body: str


class NewsletterBroadcastOut(BaseModel):
    sent: int
    failed: int
    total: int


class NewsletterSignupIn(BaseModel):
    email: str


class NewsletterSignupOut(BaseModel):
    email: str
    already_subscribed: bool


class BenchmarkPoint(BaseModel):
    """One point in a cross-market rebased comparison — this is what
    powers the hero chart on the landing page."""
    trade_date: date
    ngx_value: float | None = None
    us_value: float | None = None
