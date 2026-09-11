"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api import websocket as ws
from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.routes import auth, market, portfolio, signals, system
from app.core.config import settings
from app.core.exceptions import (
    AllProvidersFailed, InsufficientDataError, KillSwitchEngaged, ModelNotAvailable,
    RiskRejection, StaleDataError, StockIntelError, UnknownPriceError,
)
from app.core.logging import configure_logging, get_logger
from app.database.session import check_database

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            log.error("configuration_error", problem=problem)
        if settings.is_production:
            # Refuse to serve production traffic with an unsafe configuration.
            raise RuntimeError(
                "refusing to start in production with: " + "; ".join(problems)
            )

    if not check_database():
        log.error("database_unreachable_at_startup")

    relay = asyncio.create_task(ws.redis_subscriber())

    scheduler = None
    if settings.scheduler_enabled:
        from app.workers.scheduler import start_scheduler, stop_scheduler

        scheduler = start_scheduler()

    log.info(
        "application_started",
        environment=settings.environment,
        trading_mode=settings.trading_mode,
        live_trading=settings.live_trading_enabled,
        providers=settings.provider_chain,
    )
    try:
        yield
    finally:
        relay.cancel()
        if scheduler is not None:
            from app.workers.scheduler import stop_scheduler

            stop_scheduler()
        log.info("application_stopped")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "AI stock intelligence and market monitoring platform.\n\n"
        "**Not investment advice.** Signals are model estimates carrying "
        "uncertainty, not predictions of certainty. Data freshness "
        "(LIVE / DELAYED / EOD / SYNTHETIC) is reported on every price and "
        "signal; act only on what the freshness tag supports."
    ),
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_production else None,
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    max_age=600,
)


# ---------------------------------------------------------- error handling
@app.exception_handler(StaleDataError)
async def _stale_data(request: Request, exc: StaleDataError):
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "stale_data"})


@app.exception_handler(UnknownPriceError)
async def _unknown_price(request: Request, exc: UnknownPriceError):
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "unknown_price"})


@app.exception_handler(ModelNotAvailable)
async def _no_model(request: Request, exc: ModelNotAvailable):
    return JSONResponse(
        status_code=503, content={"detail": str(exc), "code": "model_unavailable"}
    )


@app.exception_handler(AllProvidersFailed)
async def _providers_failed(request: Request, exc: AllProvidersFailed):
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "code": "providers_unavailable", "errors": exc.errors},
    )


@app.exception_handler(InsufficientDataError)
async def _insufficient(request: Request, exc: InsufficientDataError):
    return JSONResponse(
        status_code=422, content={"detail": str(exc), "code": "insufficient_data"}
    )


@app.exception_handler(RiskRejection)
async def _risk(request: Request, exc: RiskRejection):
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "code": "risk_rejected"}
    )


@app.exception_handler(KillSwitchEngaged)
async def _kill_switch(request: Request, exc: KillSwitchEngaged):
    return JSONResponse(
        status_code=503, content={"detail": str(exc), "code": "kill_switch_engaged"}
    )


@app.exception_handler(StockIntelError)
async def _domain_error(request: Request, exc: StockIntelError):
    return JSONResponse(status_code=400, content={"detail": str(exc), "code": "domain_error"})


# ------------------------------------------------------------------ routes
prefix = settings.api_prefix
app.include_router(auth.router, prefix=prefix)
app.include_router(market.router, prefix=prefix)
app.include_router(signals.router, prefix=prefix)
app.include_router(portfolio.router, prefix=prefix)
app.include_router(system.router, prefix=prefix)


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "environment": settings.environment,
        "api": prefix,
        "docs": "/docs" if not settings.is_production else "disabled in production",
        "disclaimer": (
            "Model-generated estimates for research and paper trading. "
            "Not investment advice; no outcome is guaranteed."
        ),
    }


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Container liveness probe: process is up and the database answers."""
    ok = check_database()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "database unreachable"},
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Live updates: signals, alerts, quotes and pipeline events."""
    await ws.manager.connect(websocket)
    try:
        await websocket.send_json(
            {"type": "connected", "message": "subscribed to live updates"}
        )
        while True:
            # The client only needs to keep the socket alive; the server pushes.
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        await ws.manager.disconnect(websocket)
    except Exception as exc:
        log.warning("websocket_error", error=str(exc))
        await ws.manager.disconnect(websocket)
