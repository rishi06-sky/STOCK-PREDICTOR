"""Authentication endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    assert_not_locked, auth_rate_limit, get_current_user, record_audit,
    register_failed_login,
)
from app.core.config import settings
from app.core.exceptions import AuthError
from app.core.security import (
    create_access_token, create_refresh_token, decode_token, generate_api_key,
    hash_password, verify_password,
)
from app.database.session import get_db
from app.models.enums import UserRole
from app.models.platform import ApiKey, User
from app.schemas.common import (
    ApiKeyCreate, ApiKeyOut, LoginRequest, Message, RefreshRequest,
    RegisterRequest, TokenResponse, UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest, request: Request,
    db: Session = Depends(get_db), _: None = Depends(auth_rate_limit),
):
    existing = db.scalars(select(User).where(User.email == payload.email.lower())).first()
    if existing is not None:
        # Same status and shape as a weak-password rejection, so this endpoint
        # cannot be used to enumerate registered addresses.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="registration could not be completed with those details",
        )
    try:
        password_hash = hash_password(payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # The first account bootstraps the system as an administrator.
    is_first = db.scalar(select(User.id).limit(1)) is None
    user = User(
        email=payload.email.lower(), full_name=payload.full_name,
        password_hash=password_hash,
        role=UserRole.ADMIN if is_first else UserRole.USER,
    )
    db.add(user)
    db.flush()
    record_audit(
        db, actor=user.email, action="auth.register", request=request,
        resource_type="user", resource_id=str(user.id), user_id=user.id,
    )
    db.commit()
    return user


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest, request: Request,
    db: Session = Depends(get_db), _: None = Depends(auth_rate_limit),
):
    user = db.scalars(select(User).where(User.email == payload.email.lower())).first()

    # Always run a hash comparison so a missing account and a wrong password
    # take comparable time and cannot be distinguished by timing.
    if user is None:
        verify_password(payload.password, "$2b$12$" + "x" * 53)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )

    assert_not_locked(user)

    if not verify_password(payload.password, user.password_hash):
        register_failed_login(db, user)
        record_audit(
            db, actor=user.email, action="auth.login", request=request,
            success=False, user_id=user.id,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is disabled")

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    record_audit(
        db, actor=user.email, action="auth.login", request=request, user_id=user.id
    )
    db.commit()

    return TokenResponse(
        access_token=create_access_token(str(user.id), role=str(user.role)),
        refresh_token=create_refresh_token(str(user.id), role=str(user.role)),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    payload: RefreshRequest, db: Session = Depends(get_db),
    _: None = Depends(auth_rate_limit),
):
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account is unavailable"
        )
    return TokenResponse(
        access_token=create_access_token(str(user.id), role=str(user.role)),
        refresh_token=create_refresh_token(str(user.id), role=str(user.role)),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/api-keys", response_model=ApiKeyOut, status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: ApiKeyCreate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    raw, digest = generate_api_key()
    record = ApiKey(
        user_id=user.id, name=payload.name, key_hash=digest, prefix=raw[:12],
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days else None
        ),
    )
    db.add(record)
    db.flush()
    record_audit(
        db, actor=user.email, action="auth.api_key.create", request=request,
        resource_type="api_key", resource_id=str(record.id), user_id=user.id,
    )
    db.commit()
    return ApiKeyOut(
        id=record.id, name=record.name, prefix=record.prefix,
        created_at=record.created_at, expires_at=record.expires_at,
        last_used_at=None,
        # Shown once here and never stored in plaintext.
        key=raw,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
    ).all()
    return [
        ApiKeyOut(
            id=r.id, name=r.name, prefix=r.prefix, created_at=r.created_at,
            expires_at=r.expires_at, last_used_at=r.last_used_at, key=None,
        )
        for r in rows
    ]


@router.delete("/api-keys/{key_id}", response_model=Message)
def revoke_api_key(
    key_id: int, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    record = db.get(ApiKey, key_id)
    if record is None or record.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    record.revoked_at = datetime.now(timezone.utc)
    record_audit(
        db, actor=user.email, action="auth.api_key.revoke", request=request,
        resource_type="api_key", resource_id=str(key_id), user_id=user.id,
    )
    db.commit()
    return Message(message="API key revoked")
