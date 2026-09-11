"""Security headers, request logging and a request id."""
from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # The API serves JSON only; a restrictive default-src costs nothing here.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, log the outcome, and never leak internals on error."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            log.error(
                "request_failed", method=request.method, path=request.url.path,
                duration_ms=round(duration, 2), error=str(exc), exc_info=True,
            )
            structlog.contextvars.clear_contextvars()
            # Stack traces and driver messages stay in the log, not the body.
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "internal server error",
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )

        duration = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        if duration > 2000 or response.status_code >= 500:
            log.warning(
                "slow_or_failed_request", method=request.method, path=request.url.path,
                status=response.status_code, duration_ms=round(duration, 2),
            )
        structlog.contextvars.clear_contextvars()
        return response
