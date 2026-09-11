"""FastAPI dependencies: authentication, authorisation and rate limiting."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AuthError
from app.core.logging import get_logger
from app.core.security import decode_token, hash_api_key
from app.database.session import get_db
from app.models.enums import UserRole
from app.models.platform import ApiKey, AuditLog, User

log = get_logger(__name__)

bearer = HTTPBearer(auto_error=False)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


# ------------------------------------------------------------- rate limiting
class SlidingWindowLimiter:
    """In-process limiter.

    Adequate for a single API process. A multi-process deployment should move
    this to Redis so the budget is shared; the interface is unchanged.
    """

    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return False, 0
            bucket.append(now)
            return True, limit - len(bucket)


limiter = SlidingWindowLimiter()


def _client_key(request: Request) -> str:
    # X-Forwarded-For is only trustworthy behind a proxy that sets it; the
    # direct peer address is the fallback.
    forwarded = request.headers.get("x-forwarded-for", "")
    client = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    return f"{client}:{request.url.path}"


def rate_limit(request: Request) -> None:
    allowed, remaining = limiter.check(_client_key(request), settings.rate_limit_per_minute)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded; slow down",
            headers={"Retry-After": "60"},
        )


def auth_rate_limit(request: Request) -> None:
    """Tighter budget on credential endpoints, to blunt brute forcing."""
    allowed, _ = limiter.check(
        f"auth:{_client_key(request)}", settings.auth_rate_limit_per_minute
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many authentication attempts; try again shortly",
            headers={"Retry-After": "60"},
        )


# ------------------------------------------------------------- authentication
def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    user: User | None = None

    if credentials is not None and credentials.scheme.lower() == "bearer":
        try:
            payload = decode_token(credentials.credentials, expected_type="access")
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        user = db.get(User, int(payload["sub"]))

    elif x_api_key:
        record = db.scalars(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key))
        ).first()
        now = datetime.now(timezone.utc)
        if (
            record is None
            or record.revoked_at is not None
            or (record.expires_at is not None and record.expires_at < now)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired API key"
            )
        record.last_used_at = now
        db.commit()
        user = db.get(User, record.user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is disabled")
    return user


def require_role(*roles: UserRole):
    """Dependency factory enforcing a minimum role."""

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this action requires one of: {', '.join(str(r) for r in roles)}",
            )
        return user

    return _check


require_admin = require_role(UserRole.ADMIN)


def get_writable_user(user: User = Depends(get_current_user)) -> User:
    if user.role is UserRole.READONLY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this account is read-only"
        )
    return user


# -------------------------------------------------------------- audit helper
def record_audit(
    db: Session, *, actor: str, action: str, request: Request | None = None,
    resource_type: str | None = None, resource_id: str | None = None,
    success: bool = True, details: dict | None = None, user_id: int | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id, actor=actor, action=action,
            resource_type=resource_type, resource_id=resource_id,
            ip_address=(
                request.client.host if request and request.client else None
            ),
            user_agent=(
                (request.headers.get("user-agent") or "")[:256] if request else None
            ),
            success=success, details=details, created_at=datetime.now(timezone.utc),
        )
    )


def register_failed_login(db: Session, user: User) -> None:
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= MAX_FAILED_LOGINS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        log.warning("account_locked", email=user.email, minutes=LOCKOUT_MINUTES)


def assert_not_locked(user: User) -> None:
    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        remaining = (user.locked_until - datetime.now(timezone.utc)).total_seconds() / 60
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"account is locked for another {remaining:.0f} minute(s)",
        )
