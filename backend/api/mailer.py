"""
Email sending via Resend (resend.com) — chosen over raw SMTP because
transactional auth emails (verification, password reset) need
reliable delivery, and Gmail-style SMTP throttles hard and isn't
built for this. Resend's free tier covers auth-email volume for an
MVP; the newsletter send path is the one place volume could matter,
flagged below.

RESEND_API_KEY must be set — without it, send_email() logs and
no-ops rather than crashing the calling request (registration/reset
flows shouldn't 500 just because email delivery is unconfigured in a
dev environment).
"""
import logging
import os

import requests

log = logging.getLogger("email")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_API_URL = "https://api.resend.com/emails"
FROM_ADDRESS = os.environ.get("EMAIL_FROM", "MarketMaven <onboarding@resend.dev>")


def send_email(to: str, subject: str, html_body: str) -> bool:
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping email send to %s (subject: %s)", to, subject)
        return False

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": FROM_ADDRESS, "to": [to], "subject": subject, "html": html_body},
            timeout=10,
        )
        if resp.status_code >= 400:
            log.error("Resend API error %s sending to %s: %s", resp.status_code, to, resp.text)
            return False
        return True
    except requests.RequestException as exc:
        log.error("Email send failed for %s: %s", to, exc)
        return False


def send_verification_email(to: str, token: str, frontend_base_url: str) -> bool:
    verify_url = f"{frontend_base_url}/verify-email?token={token}"
    html = f"""
    <p>Welcome to MarketMaven — confirm your email to get started.</p>
    <p><a href="{verify_url}">Verify your email</a></p>
    <p>If you didn't create this account, you can ignore this email.</p>
    """
    return send_email(to, "Verify your MarketMaven account", html)


def send_password_reset_email(to: str, token: str, frontend_base_url: str) -> bool:
    reset_url = f"{frontend_base_url}/reset-password?token={token}"
    html = f"""
    <p>Reset your MarketMaven password.</p>
    <p><a href="{reset_url}">Choose a new password</a></p>
    <p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>
    """
    return send_email(to, "Reset your MarketMaven password", html)


def send_newsletter_broadcast(recipients: list[str], subject: str, html_body: str) -> dict:
    """Uses Resend's batch endpoint (/emails/batch, up to 100 messages
    per call) instead of one HTTP request per recipient — meaningfully
    fewer round-trips and far less likely to hit rate limits at any
    real subscriber count. Falls back to individual sends only if
    RESEND_API_KEY isn't set, matching send_email()'s no-op behavior."""
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping newsletter send to %d recipients", len(recipients))
        return {"sent": 0, "failed": len(recipients), "total": len(recipients)}

    BATCH_SIZE = 100
    sent, failed = 0, 0

    for i in range(0, len(recipients), BATCH_SIZE):
        batch = recipients[i:i + BATCH_SIZE]
        payload = [
            {"from": FROM_ADDRESS, "to": [email], "subject": subject, "html": html_body}
            for email in batch
        ]
        try:
            resp = requests.post(
                f"{RESEND_API_URL}/batch",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
                timeout=20,
            )
            if resp.status_code >= 400:
                log.error("Resend batch API error %s for batch starting at %d: %s", resp.status_code, i, resp.text)
                failed += len(batch)
            else:
                sent += len(batch)
        except requests.RequestException as exc:
            log.error("Newsletter batch send failed for batch starting at %d: %s", i, exc)
            failed += len(batch)

    return {"sent": sent, "failed": failed, "total": len(recipients)}
