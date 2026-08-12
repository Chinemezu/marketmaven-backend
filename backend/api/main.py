"""
Marketmaven data API.

This is the boundary the dashboard and the Claude Q&A layer both sit
behind. Keep it that way — the Q&A layer should call these endpoints
(or equivalent internal functions), not run raw SQL against the DB
directly. That's what keeps natural-language queries safe and
predictable once that layer gets built (Phase 4 in the pipeline doc).
"""
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import Session, aliased

from database import get_db
from models import (
    Issuer, PriceDaily, IndexDaily, Filing, Insight, PeerMapping, NewsletterSignup,
    User, WatchlistItem, SavedArticle, Report,
)
import auth as auth_utils
import mailer
from schemas import (
    IssuerOut, PriceOut, IndexOut, FilingOut, InsightOut, PeerMappingOut,
    SourceRank, NewsletterSignupIn, NewsletterSignupOut, BenchmarkPoint,
    UserRegisterIn, UserLoginIn, UserOut, TokenOut, ForgotPasswordIn,
    ResetPasswordIn, MessageOut, WatchlistItemOut, WatchlistAddIn,
    SavedArticleOut, AdminFeatureIn, NewsletterBroadcastIn, NewsletterBroadcastOut,
    ReportOut, ReportDetailOut, ReportCreateIn, ReportUpdateIn, EditorsPickOut, ReportSendNewsletterOut,
)

app = FastAPI(title="Marketmaven API", version="0.1.0")

# CORS: comma-separated origins via env var, e.g. "https://marketmaven.app,https://staging.marketmaven.app".
# Defaults to localhost so local frontend dev works without any setup;
# set FRONTEND_ORIGINS for real before this is public.
_origins = os.environ.get("FRONTEND_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_credentials=True,  # not currently used (Bearer token, not cookies) but harmless to allow
    allow_headers=["*"],
)

# Rate limiting: 60 req/min per IP by default, overridable via env var.
# Keeps a public read-only API from being trivially expensive to abuse
# without requiring auth for launch.
limiter = Limiter(key_func=get_remote_address, default_limits=[os.environ.get("RATE_LIMIT", "60/minute")])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")

# --- Auth dependency ---
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = auth_utils.decode_access_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# --- Auth routes ---

@app.post("/auth/register", response_model=TokenOut)
def register(payload: UserRegisterIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if db.query(User).filter(User.email == email).one_or_none():
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    verification_token = auth_utils.generate_token()
    user = User(
        email=email,
        password_hash=auth_utils.hash_password(payload.password),
        verification_token=verification_token,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    mailer.send_verification_email(email, verification_token, FRONTEND_BASE_URL)

    token = auth_utils.create_access_token(user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/auth/login", response_model=TokenOut)
def login(payload: UserLoginIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or not auth_utils.verify_password(payload.password, user.password_hash):
        # Same message for "no such user" and "wrong password" — don't
        # leak which one it was.
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = auth_utils.create_access_token(user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.get("/auth/me", response_model=UserOut)
def get_me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@app.post("/auth/verify-email", response_model=MessageOut)
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.verification_token == token).one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or already-used verification token")
    user.is_verified = True
    user.verification_token = None
    db.commit()
    return MessageOut(message="Email verified")


@app.post("/auth/forgot-password", response_model=MessageOut)
def forgot_password(payload: ForgotPasswordIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).one_or_none()
    # Always return the same message whether or not the account exists —
    # otherwise this endpoint becomes a way to check who's registered.
    if user is not None:
        reset_token = auth_utils.generate_token()
        user.reset_token = reset_token
        user.reset_token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()
        mailer.send_password_reset_email(email, reset_token, FRONTEND_BASE_URL)
    return MessageOut(message="If an account with that email exists, a reset link has been sent")


@app.post("/auth/reset-password", response_model=MessageOut)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.reset_token == payload.token).one_or_none()
    if user is None or user.reset_token_expires is None or user.reset_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    user.password_hash = auth_utils.hash_password(payload.new_password)
    user.reset_token = None
    user.reset_token_expires = None
    db.commit()
    return MessageOut(message="Password reset — you can now log in with your new password")


# --- Watchlist routes (plain watchlist — no quantity/P&L, per confirmed scope) ---

@app.get("/watchlist", response_model=list[WatchlistItemOut])
def get_watchlist(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = (
        select(WatchlistItem, Issuer)
        .join(Issuer, WatchlistItem.issuer_id == Issuer.id)
        .where(WatchlistItem.user_id == user.id)
    )
    rows = db.execute(stmt).all()
    return [
        WatchlistItemOut(issuer_id=issuer.id, ticker=issuer.ticker, name=issuer.name, exchange=issuer.exchange)
        for _, issuer in rows
    ]


@app.post("/watchlist", response_model=WatchlistItemOut)
def add_to_watchlist(payload: WatchlistAddIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    issuer = db.get(Issuer, payload.issuer_id)
    if issuer is None:
        raise HTTPException(status_code=404, detail="Issuer not found")

    existing = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.issuer_id == payload.issuer_id)
        .one_or_none()
    )
    if not existing:
        db.add(WatchlistItem(user_id=user.id, issuer_id=payload.issuer_id))
        db.commit()

    return WatchlistItemOut(issuer_id=issuer.id, ticker=issuer.ticker, name=issuer.name, exchange=issuer.exchange)


@app.delete("/watchlist/{issuer_id}", response_model=MessageOut)
def remove_from_watchlist(issuer_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.issuer_id == issuer_id)
        .one_or_none()
    )
    if item:
        db.delete(item)
        db.commit()
    return MessageOut(message="Removed from watchlist")


# --- Saved articles routes ---

@app.get("/saved-articles", response_model=list[SavedArticleOut])
def get_saved_articles(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = (
        select(SavedArticle, Insight)
        .join(Insight, SavedArticle.insight_id == Insight.id)
        .where(SavedArticle.user_id == user.id)
        .order_by(SavedArticle.saved_at.desc())
    )
    rows = db.execute(stmt).all()
    return [
        SavedArticleOut(
            id=insight.id, source=insight.source, vertical=insight.vertical, title=insight.title,
            url=insight.url, published_date=insight.published_date, summary=insight.summary,
            image_url=insight.image_url, saved_at=saved.saved_at,
        )
        for saved, insight in rows
    ]


@app.post("/saved-articles/{insight_id}", response_model=MessageOut)
def save_article(insight_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(status_code=404, detail="Article not found")

    existing = (
        db.query(SavedArticle)
        .filter(SavedArticle.user_id == user.id, SavedArticle.insight_id == insight_id)
        .one_or_none()
    )
    if not existing:
        db.add(SavedArticle(user_id=user.id, insight_id=insight_id))
        db.commit()
    return MessageOut(message="Saved")


@app.delete("/saved-articles/{insight_id}", response_model=MessageOut)
def unsave_article(insight_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = (
        db.query(SavedArticle)
        .filter(SavedArticle.user_id == user.id, SavedArticle.insight_id == insight_id)
        .one_or_none()
    )
    if item:
        db.delete(item)
        db.commit()
    return MessageOut(message="Removed from saved articles")


# --- Admin routes ---

@app.patch("/admin/insights/{insight_id}/feature", response_model=InsightOut)
def admin_set_featured(
    insight_id: int,
    payload: AdminFeatureIn,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Backs the admin panel's feature/pin control — this is the manual
    half of the hybrid curation model."""
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(status_code=404, detail="Article not found")
    insight.featured = payload.featured
    insight.featured_order = payload.featured_order
    db.commit()
    db.refresh(insight)
    return insight


@app.get("/admin/newsletter-signups", response_model=list[str])
def admin_list_newsletter_signups(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(NewsletterSignup.email)).scalars().all()
    return rows


@app.post("/admin/newsletter/send", response_model=NewsletterBroadcastOut)
def admin_send_newsletter(
    payload: NewsletterBroadcastIn,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    recipients = db.execute(select(NewsletterSignup.email)).scalars().all()
    result = mailer.send_newsletter_broadcast(list(recipients), payload.subject, payload.html_body)
    return NewsletterBroadcastOut(**result)


# --- Reports (first-party editorial content) ---

def _slugify(title: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:240]


def _report_out(report: Report, detail: bool = False):
    data = dict(
        id=report.id, slug=report.slug, title=report.title,
        author_name=report.author.email.split("@")[0],  # display name — swap for a real name field on User if one gets added later
        vertical=report.vertical, summary=report.summary,
        cover_image_url=report.cover_image_url, status=report.status,
        featured=report.featured, featured_order=report.featured_order,
        published_at=report.published_at,
    )
    if detail:
        data["body"] = report.body
        return ReportDetailOut(**data)
    return ReportOut(**data)


@app.get("/reports", response_model=list[ReportOut])
def list_reports(
    vertical: str | None = None,
    newsletter_only: bool = Query(False, description="True to list only newsletter-tagged reports — backs the Newsletters page"),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(Report).where(Report.status == "published")
    if vertical:
        stmt = stmt.where(Report.vertical == vertical)
    if newsletter_only:
        stmt = stmt.where(Report.is_newsletter == True)  # noqa: E712
    stmt = stmt.order_by(Report.featured.desc(), Report.featured_order.asc().nulls_last(), Report.published_at.desc())
    reports = db.execute(stmt.limit(limit)).scalars().all()
    return [_report_out(r) for r in reports]


@app.get("/reports/{slug}", response_model=ReportDetailOut)
def get_report(slug: str, db: Session = Depends(get_db)):
    report = db.query(Report).filter(Report.slug == slug, Report.status == "published").one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return _report_out(report, detail=True)


@app.get("/admin/reports", response_model=list[ReportOut])
def admin_list_reports(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Unlike GET /reports, includes drafts — backs the admin panel's
    report list, which needs to show unpublished work in progress."""
    stmt = select(Report).order_by(Report.updated_at.desc())
    reports = db.execute(stmt).scalars().all()
    return [_report_out(r) for r in reports]


@app.post("/admin/reports", response_model=ReportDetailOut)
def admin_create_report(payload: ReportCreateIn, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    slug = _slugify(payload.title)
    if db.query(Report).filter(Report.slug == slug).one_or_none():
        slug = f"{slug}-{auth_utils.generate_token(4)}"

    status = payload.status or "draft"
    report = Report(
        slug=slug, title=payload.title, author_id=admin.id, vertical=payload.vertical,
        summary=payload.summary, body=payload.body, cover_image_url=payload.cover_image_url,
        status=status, featured=payload.featured or False, featured_order=payload.featured_order,
    )
    # Same publish-sets-published_at behavior as admin_update_report,
    # otherwise a report created directly as "published" (the admin form's
    # normal one-step flow) would have no published_at and never appear
    # in GET /reports, which orders by it.
    if status == "published":
        report.published_at = datetime.now(timezone.utc)

    db.add(report)
    db.commit()
    db.refresh(report)
    return _report_out(report, detail=True)


@app.patch("/admin/reports/{report_id}", response_model=ReportDetailOut)
def admin_update_report(
    report_id: int,
    payload: ReportUpdateIn,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    updates = payload.model_dump(exclude_unset=True)
    was_draft = report.status != "published"

    for field, value in updates.items():
        setattr(report, field, value)

    # Publishing for the first time sets published_at — republishing an
    # edit to an already-published report shouldn't reset its date.
    if report.status == "published" and was_draft and report.published_at is None:
        report.published_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(report)
    return _report_out(report, detail=True)


@app.delete("/admin/reports/{report_id}", response_model=MessageOut)
def admin_delete_report(report_id: int, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report:
        db.delete(report)
        db.commit()
    return MessageOut(message="Report deleted")


@app.post("/admin/reports/{report_id}/send-as-newsletter", response_model=ReportSendNewsletterOut)
def admin_send_report_as_newsletter(
    report_id: int,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """The one-click action tying the two distribution channels
    together: takes an existing report's title/summary/body and
    broadcasts it to the newsletter list via the same batch-send path
    used elsewhere. Requires the report to already be published — a
    draft shouldn't go out over email before it's live on the site."""
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "published":
        raise HTTPException(status_code=400, detail="Publish the report before sending it as a newsletter")

    recipients = db.execute(select(NewsletterSignup.email)).scalars().all()
    html = f"<h2>{report.title}</h2><p>{report.summary}</p><div>{report.body}</div>"
    result = mailer.send_newsletter_broadcast(list(recipients), report.title, html)

    report.is_newsletter = True
    report.newsletter_sent_at = datetime.now(timezone.utc)
    db.commit()

    return ReportSendNewsletterOut(report_id=report.id, **result)


@app.get("/editors-picks", response_model=list[EditorsPickOut])
def editors_picks(limit: int = Query(10, le=50), db: Session = Depends(get_db)):
    """Unified curation feed: featured Insights (aggregated, pinned) and
    featured Reports (first-party) ranked together by featured_order.
    This is 'Editor's Picks' as a genuinely mixed feed, not two separate
    modules the frontend has to reconcile itself."""
    insight_rows = db.execute(
        select(Insight).where(Insight.featured == True).order_by(Insight.featured_order.asc().nulls_last())  # noqa: E712
    ).scalars().all()
    report_rows = db.execute(
        select(Report).where(Report.featured == True, Report.status == "published").order_by(Report.featured_order.asc().nulls_last())  # noqa: E712
    ).scalars().all()

    picks = [
        EditorsPickOut(
            content_type="insight", id=i.id, title=i.title, summary=i.summary, vertical=i.vertical,
            source_or_author=i.source, url_or_slug=i.url,
            featured_order=i.featured_order, published_date=i.published_date,
        )
        for i in insight_rows
    ] + [
        EditorsPickOut(
            content_type="report", id=r.id, title=r.title, summary=r.summary, vertical=r.vertical,
            source_or_author=r.author.email.split("@")[0], url_or_slug=r.slug,
            featured_order=r.featured_order, published_date=r.published_at,
        )
        for r in report_rows
    ]

    picks.sort(key=lambda p: (p.featured_order is None, p.featured_order or 0))
    return picks[:limit]


@app.get("/issuers", response_model=list[IssuerOut])
def list_issuers(
    exchange: str | None = Query(None, description="e.g. NGX, NYSE, NASDAQ"),
    sector: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Issuer)
    if exchange:
        stmt = stmt.where(Issuer.exchange == exchange)
    if sector:
        stmt = stmt.where(Issuer.sector == sector)
    return db.execute(stmt).scalars().all()


@app.get("/issuers/{issuer_id}/prices", response_model=list[PriceOut])
def issuer_prices(
    issuer_id: int,
    start: date | None = None,
    end: date | None = None,
    db: Session = Depends(get_db),
):
    issuer = db.get(Issuer, issuer_id)
    if not issuer:
        raise HTTPException(status_code=404, detail="Issuer not found")

    stmt = select(PriceDaily).where(PriceDaily.issuer_id == issuer_id)
    if start:
        stmt = stmt.where(PriceDaily.trade_date >= start)
    if end:
        stmt = stmt.where(PriceDaily.trade_date <= end)
    stmt = stmt.order_by(PriceDaily.trade_date)

    return db.execute(stmt).scalars().all()


@app.get("/indices/{index_code}", response_model=list[IndexOut])
def index_series(
    index_code: str,
    start: date | None = None,
    end: date | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(IndexDaily).where(IndexDaily.index_code == index_code)
    if start:
        stmt = stmt.where(IndexDaily.trade_date >= start)
    if end:
        stmt = stmt.where(IndexDaily.trade_date <= end)
    stmt = stmt.order_by(IndexDaily.trade_date)

    rows = db.execute(stmt).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data for index '{index_code}'")
    return rows


@app.get("/filings", response_model=list[FilingOut])
def list_filings(
    issuer_id: int | None = None,
    source: str | None = Query(None, description="NGX, SEC_NG, or EDGAR"),
    since: date | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Filing)
    if issuer_id:
        stmt = stmt.where(Filing.issuer_id == issuer_id)
    if source:
        stmt = stmt.where(Filing.source == source)
    if since:
        stmt = stmt.where(Filing.filing_date >= since)
    stmt = stmt.order_by(Filing.filing_date.desc())

    return db.execute(stmt).scalars().all()


@app.get("/insights", response_model=list[InsightOut])
def list_insights(
    vertical: str | None = Query(None, description="e.g. 'finance', 'entertainment', 'sports' — omit for all"),
    min_score: int = Query(1, description="Minimum relevance score to include"),
    sort: str = Query("relevance", description="'relevance' (featured-first, then score) or 'recent' (pure published_date)"),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    """Backs two different UI needs with one endpoint:
    - sort=relevance (default): featured items first, then by relevance
      score, then recency — powers the hero/spotlight/breaking-news modules.
    - sort=recent: pure published_date ordering, ignoring featured status
      and score — powers a dense "latest headlines" list (e.g. Flash News),
      which should show what's newest, not what's most prominent.
    """
    stmt = select(Insight).where(Insight.relevance_score >= min_score)
    if vertical:
        stmt = stmt.where(Insight.vertical == vertical)

    if sort == "recent":
        stmt = stmt.order_by(Insight.published_date.desc())
    else:
        stmt = stmt.order_by(
            Insight.featured.desc(),
            Insight.featured_order.asc().nulls_last(),
            Insight.relevance_score.desc(),
            Insight.published_date.desc(),
        )

    return db.execute(stmt.limit(limit)).scalars().all()


@app.get("/insights/top-sources", response_model=list[SourceRank])
def top_sources(
    vertical: str | None = None,
    limit: int = Query(10, le=50),
    db: Session = Depends(get_db),
):
    """Backs the 'Top Sources' module — ranks feed sources (Nairametrics,
    Reuters Markets, etc.) by how many stored articles came from each.
    Real data, not a stand-in for the 'Top Authors' pattern in the
    reference designs, which assumed personal bylines we don't have."""
    stmt = select(Insight.source, func.count(Insight.id).label("article_count"))
    if vertical:
        stmt = stmt.where(Insight.vertical == vertical)
    stmt = stmt.group_by(Insight.source).order_by(func.count(Insight.id).desc()).limit(limit)

    rows = db.execute(stmt).all()
    return [SourceRank(source=source, article_count=count) for source, count in rows]


@app.post("/newsletter-signup", response_model=NewsletterSignupOut)
def newsletter_signup(payload: NewsletterSignupIn, db: Session = Depends(get_db)):
    """Lightweight email capture for the hero band and footer signup —
    no auth system required, just a deduplicated email list."""
    email = payload.email.strip().lower()
    existing = db.query(NewsletterSignup).filter(NewsletterSignup.email == email).one_or_none()
    if existing:
        return NewsletterSignupOut(email=email, already_subscribed=True)

    db.add(NewsletterSignup(email=email))
    db.commit()
    return NewsletterSignupOut(email=email, already_subscribed=False)


@app.get("/peer-mappings", response_model=list[PeerMappingOut])
def list_peer_mappings(
    sector: str | None = None,
    ngx_ticker: str | None = None,
    db: Session = Depends(get_db),
):
    """Backs any sector/peer-comparison UI (e.g. 'NGX banking vs US
    financials'). Built as a manual join rather than from_attributes,
    since the response shape flattens both sides of the mapping —
    PeerMapping itself only stores the two issuer FKs."""
    NgxIssuer = aliased(Issuer)
    UsIssuer = aliased(Issuer)

    stmt = (
        select(PeerMapping, NgxIssuer, UsIssuer)
        .join(NgxIssuer, PeerMapping.ngx_issuer_id == NgxIssuer.id)
        .join(UsIssuer, PeerMapping.us_peer_issuer_id == UsIssuer.id)
    )
    if sector:
        stmt = stmt.where(PeerMapping.sector == sector)
    if ngx_ticker:
        stmt = stmt.where(NgxIssuer.ticker == ngx_ticker)

    rows = db.execute(stmt).all()
    return [
        PeerMappingOut(
            ngx_ticker=ngx.ticker, ngx_name=ngx.name,
            us_ticker=us.ticker, us_name=us.name,
            sector=mapping.sector, mapping_confidence=float(mapping.mapping_confidence) if mapping.mapping_confidence is not None else None,
        )
        for mapping, ngx, us in rows
    ]


@app.get("/benchmark", response_model=list[BenchmarkPoint])
def benchmark(
    ngx_index: str = Query(..., description="e.g. NGX_ASI"),
    us_index: str = Query(..., description="e.g. SPX or IXIC"),
    start: date = Query(..., description="Rebase date — both series start at 100 here"),
    end: date | None = None,
    db: Session = Depends(get_db),
):
    """Powers the landing page's rebased comparison chart. Both series
    are rebased to 100 at `start` so absolute index-point differences
    between NGX and US indices don't distort the comparison."""

    def series(code: str) -> list[IndexDaily]:
        stmt = select(IndexDaily).where(IndexDaily.index_code == code, IndexDaily.trade_date >= start)
        if end:
            stmt = stmt.where(IndexDaily.trade_date <= end)
        return db.execute(stmt.order_by(IndexDaily.trade_date)).scalars().all()

    ngx_rows = series(ngx_index)
    us_rows = series(us_index)
    if not ngx_rows or not us_rows:
        raise HTTPException(status_code=404, detail="Insufficient data for one or both indices in this range")

    ngx_base = float(ngx_rows[0].value)
    us_base = float(us_rows[0].value)
    ngx_by_date = {r.trade_date: float(r.value) / ngx_base * 100 for r in ngx_rows}
    us_by_date = {r.trade_date: float(r.value) / us_base * 100 for r in us_rows}

    all_dates = sorted(set(ngx_by_date) | set(us_by_date))
    return [
        BenchmarkPoint(trade_date=d, ngx_value=ngx_by_date.get(d), us_value=us_by_date.get(d))
        for d in all_dates
    ]
