"""
Auth utilities: password hashing (bcrypt via passlib) and JWT issuing
/verification. Deliberately simple per the confirmed decision
(email/password, keep it simple) — no OAuth, no magic links.

JWT_SECRET must be set to a real random value in production — the
default here is a placeholder and would let anyone forge tokens if
shipped as-is. Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

JWT_SECRET = os.environ.get("JWT_SECRET", "CHANGE-ME-INSECURE-DEFAULT")
JWT_ALGORITHM = "HS256"
JWT_EXPIRES_HOURS = int(os.environ.get("JWT_EXPIRES_HOURS", "168"))  # 7 days


def hash_password(plain_password: str) -> str:
    # bcrypt has a hard 72-byte input limit — truncate rather than let
    # it raise, since a user's actual password rarely exceeds this and
    # failing registration outright on a long paste is worse UX than a
    # silent, standard truncation (same behavior passlib used to paper over).
    password_bytes = plain_password.encode("utf-8")[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    password_bytes = plain_password.encode("utf-8")[:72]
    return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRES_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """Returns the user_id if the token is valid, None otherwise —
    callers treat None as 401, not as an exception to handle."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


def generate_token(n_bytes: int = 32) -> str:
    """For email-verification and password-reset tokens — separate from
    JWTs since these are single-use, stored server-side, and checked by
    exact match rather than decoded."""
    return secrets.token_urlsafe(n_bytes)
