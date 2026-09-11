"""Password hashing and JWT issuance."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import AuthError

ALGORITHM = "HS256"


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    _assert_password_policy(password)
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(_prehash(password), salt).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _prehash(password: str) -> bytes:
    """bcrypt silently truncates input at 72 bytes.

    Pre-hashing to a fixed-length hex digest means long passphrases keep their
    full entropy instead of collapsing onto a shared 72-byte prefix.
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def _assert_password_policy(password: str) -> None:
    if len(password) < 10:
        raise AuthError("password must be at least 10 characters")
    if password.lower() in {"password12", "1234567890", "letmeinnow"}:
        raise AuthError("password is too common")


# ---------------------------------------------------------------------- jwt
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(subject: str, *, role: str = "user") -> str:
    return _encode(subject, "access", timedelta(minutes=settings.access_token_ttl_minutes), role)


def create_refresh_token(subject: str, *, role: str = "user") -> str:
    return _encode(subject, "refresh", timedelta(days=settings.refresh_token_ttl_days), role)


def _encode(subject: str, kind: str, ttl: timedelta, role: str) -> str:
    now = _now()
    payload = {
        "sub": str(subject),
        "typ": kind,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str, *, expected_type: str = "access") -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc
    if payload.get("typ") != expected_type:
        raise AuthError(f"expected a {expected_type} token")
    return payload


# ------------------------------------------------------------------ api keys
def generate_api_key() -> tuple[str, str]:
    """Return (plaintext, sha256 digest). Only the digest is ever stored."""
    raw = f"sk_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
