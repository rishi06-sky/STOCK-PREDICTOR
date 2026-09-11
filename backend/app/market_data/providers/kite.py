"""Zerodha Kite Connect v3 -- REST adapter.

This is the first provider in the platform that can legitimately emit
`DataQuality.LIVE`: Kite is an exchange-licensed feed, not a delayed
consumer mirror. Freshness of an individual datum is still judged separately
by its `source_timestamp`, so a licensed feed serving yesterday's close after
hours is correctly treated as stale by the downstream guards.

Scope: market data only. Kite is a *broker* API that can also place orders;
none of that surface is implemented here, deliberately. Trading through Kite
would mean implementing `app.trading.brokers.base.Broker`, which is a separate
decision about real money.

Authentication is a daily ritual imposed by the vendor, not by this code:

    1. Send the user to
       https://kite.zerodha.com/connect/login?api_key=<key>&v=3
    2. Zerodha redirects back with a `request_token`
    3. Exchange it here for an `access_token` (see `exchange_request_token`)
    4. The access token expires each morning (~07:30 IST); repeat

Costs, plainly: Kite Connect is a paid subscription (about Rs 2,000/month),
and historical candle data is a further paid add-on. Without the historical
add-on, `fetch_daily_bars` raises a clear error rather than returning nothing
and letting the caller guess why.
"""
from __future__ import annotations

import csv
import hashlib
import io
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.exceptions import (
    ProviderError, ProviderRateLimited, ProviderUnavailable, SymbolNotFound,
)
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider
from app.market_data.types import Bar, IntradayBar, QuoteData, SecurityInfo
from app.models.enums import DataQuality

log = get_logger(__name__)

API_ROOT = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
KITE_VERSION = "3"
IST = ZoneInfo("Asia/Kolkata")

#: Kite candle intervals. The platform's own interval names map onto these.
INTERVAL_MAP = {
    "1m": "minute", "3m": "3minute", "5m": "5minute", "10m": "10minute",
    "15m": "15minute", "30m": "30minute", "60m": "60minute", "1h": "60minute",
    "1d": "day",
}

#: Kite's historical endpoint caps the span per request by interval.
MAX_SPAN_DAYS = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}

EXCHANGE_PREFIX = {"NSE": "NSE", "BSE": "BSE", "NFO": "NFO", "CDS": "CDS", "MCX": "MCX"}


class KiteAuthError(ProviderError):
    """The access token is missing, expired or rejected.

    Distinct from a transport failure: the remedy is a human re-login, so it
    is never retried.
    """

    def __init__(self, message: str):
        super().__init__("kite", message, retryable=False)


class KiteSubscriptionError(ProviderError):
    """The account lacks the subscription this endpoint requires."""

    def __init__(self, message: str):
        super().__init__("kite", message, retryable=False)


def build_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    """SHA-256 of api_key + request_token + api_secret, as Kite requires."""
    return hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode("utf-8")
    ).hexdigest()


def login_url(api_key: str | None = None) -> str:
    return LOGIN_URL.format(api_key=api_key or settings.kite_api_key or "")


class KiteConnectProvider(MarketDataProvider):
    name = "kite"
    #: Exchange-licensed real-time feed.
    default_quality = DataQuality.LIVE
    requires_key = True
    health_check_symbol = "RELIANCE"

    def __init__(
        self, api_key: str | None = None, access_token: str | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.api_key = api_key or settings.kite_api_key
        self.access_token = access_token or settings.kite_access_token
        self.rate_limit_per_minute = settings.kite_rate_limit_per_minute
        self._instruments: dict[str, dict] | None = None
        self._instruments_loaded_at: datetime | None = None
        self._instruments_lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def is_configured(self) -> bool:
        return bool(self.api_key and self.access_token)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Kite-Version": KITE_VERSION,
            "Authorization": f"token {self.api_key}:{self.access_token}",
        }

    def exchange_request_token(self, request_token: str, api_secret: str) -> dict:
        """Trade a request_token for an access_token.

        Call once per day after the user completes the Zerodha login redirect.
        The returned access_token must be persisted by the caller -- this
        adapter does not write it to disk or to the environment.
        """
        if not self.api_key:
            raise KiteAuthError("no API key configured")

        response = self._request(
            "POST", f"{API_ROOT}/session/token",
            headers={"X-Kite-Version": KITE_VERSION},
            data={
                "api_key": self.api_key,
                "request_token": request_token,
                "checksum": build_checksum(self.api_key, request_token, api_secret),
            },
        )
        payload = self._unwrap(response)
        self.access_token = payload.get("access_token")
        if not self.access_token:
            raise KiteAuthError("session response contained no access_token")
        log.info(
            "kite_session_established",
            user_id=payload.get("user_id"), login_time=payload.get("login_time"),
        )
        return payload

    def _unwrap(self, response) -> dict:
        """Return the `data` body, translating Kite's error envelope."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, f"non-JSON response: {exc}") from exc

        if payload.get("status") == "error":
            message = payload.get("message", "unknown error")
            error_type = payload.get("error_type", "")
            if error_type in ("TokenException", "PermissionException"):
                raise KiteAuthError(
                    f"{message} (access tokens expire each morning; re-login required)"
                )
            if error_type == "DataException" and "subscription" in message.lower():
                raise KiteSubscriptionError(message)
            if error_type == "NetworkException":
                raise ProviderUnavailable(self.name, message)
            if "rate" in message.lower() or error_type == "InputException" and "limit" in message.lower():
                raise ProviderRateLimited(self.name, message)
            raise ProviderError(self.name, f"{error_type}: {message}", retryable=False)

        data = payload.get("data")
        if data is None:
            raise ProviderError(self.name, "response contained no data block")
        return data

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.is_configured():
            raise KiteAuthError(
                "KITE_API_KEY and KITE_ACCESS_TOKEN must both be set; the access "
                "token is obtained daily via the Zerodha login flow"
            )
        response = self._request(
            "GET", f"{API_ROOT}{path}", params=params, headers=self._headers
        )
        return self._unwrap(response)

    # ----------------------------------------------------------- instruments
    def load_instruments(self, exchange: str = "NSE", *, force: bool = False) -> dict[str, dict]:
        """Fetch and cache the instrument dump for an exchange.

        Kite addresses instruments by numeric token, not by symbol, so this
        mapping is required before anything else works. The dump changes at
        most daily, so it is cached for the session.
        """
        with self._instruments_lock:
            fresh = (
                self._instruments is not None
                and self._instruments_loaded_at is not None
                and datetime.now(timezone.utc) - self._instruments_loaded_at < timedelta(hours=12)
            )
            if fresh and not force:
                return self._instruments

        if not self.is_configured():
            raise KiteAuthError("cannot load instruments without API credentials")

        response = self._request(
            "GET", f"{API_ROOT}/instruments/{exchange}", headers=self._headers
        )
        text = response.text
        if text.lstrip().startswith("{"):
            # An error arrives as JSON where CSV was expected.
            self._unwrap(response)

        table: dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(text)):
            symbol = (row.get("tradingsymbol") or "").strip()
            token = row.get("instrument_token")
            if not symbol or not token:
                continue
            try:
                table[f"{exchange}:{symbol}"] = {
                    "instrument_token": int(token),
                    "tradingsymbol": symbol,
                    "name": (row.get("name") or symbol).strip(),
                    "exchange": row.get("exchange") or exchange,
                    "segment": row.get("segment"),
                    "instrument_type": row.get("instrument_type"),
                    "tick_size": float(row.get("tick_size") or 0) or None,
                    "lot_size": int(float(row.get("lot_size") or 0)) or None,
                }
            except (TypeError, ValueError):
                continue

        with self._instruments_lock:
            self._instruments = {**(self._instruments or {}), **table}
            self._instruments_loaded_at = datetime.now(timezone.utc)
            log.info("kite_instruments_loaded", exchange=exchange, count=len(table))
            return self._instruments

    def instrument_token(self, symbol: str, exchange: str = "NSE") -> int:
        """Resolve a trading symbol to its instrument token."""
        key = symbol if ":" in symbol else f"{exchange}:{symbol}"
        table = self._instruments or {}
        if key not in table:
            table = self.load_instruments(key.split(":", 1)[0])
        record = table.get(key)
        if record is None:
            raise SymbolNotFound(self.name, key)
        return record["instrument_token"]

    @staticmethod
    def to_kite_symbol(symbol: str, exchange: str = "NSE") -> str:
        """Map the platform's symbol convention onto Kite's EXCHANGE:SYMBOL."""
        if ":" in symbol:
            return symbol
        upper = symbol.upper()
        if upper.endswith(".NS"):
            return f"NSE:{upper[:-3]}"
        if upper.endswith(".BO"):
            return f"BSE:{upper[:-3]}"
        return f"{EXCHANGE_PREFIX.get(exchange.upper(), 'NSE')}:{upper}"

    # ---------------------------------------------------------------- quotes
    def fetch_quote(self, symbol: str) -> QuoteData:
        key = self.to_kite_symbol(symbol)
        data = self._get("/quote", params={"i": key})
        record = data.get(key)
        if record is None:
            raise SymbolNotFound(self.name, key)

        ohlc = record.get("ohlc") or {}
        last_price = record.get("last_price")
        if last_price is None:
            raise ProviderError(self.name, f"quote for {key} carried no last_price")

        # Kite stamps exchange time in IST without an offset; attach it rather
        # than letting it be read as UTC, which would make the quote look an
        # hour and a half fresher than it is.
        struck = _parse_ist(record.get("exchange_timestamp") or record.get("timestamp"))

        return QuoteData(
            symbol=symbol,
            price=last_price,
            source_timestamp=struck or datetime.now(timezone.utc),
            provider=self.name,
            quality=DataQuality.LIVE,
            previous_close=ohlc.get("close"),
            day_open=ohlc.get("open"),
            day_high=ohlc.get("high"),
            day_low=ohlc.get("low"),
            volume=record.get("volume"),
            currency="INR",
        )

    def fetch_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        """Batch quote lookup. Kite accepts up to 500 instruments per call."""
        if not symbols:
            return {}
        keys = [self.to_kite_symbol(s) for s in symbols[:500]]
        data = self._get("/quote", params=[("i", k) for k in keys])

        out: dict[str, QuoteData] = {}
        for original, key in zip(symbols, keys):
            record = data.get(key)
            if not record or record.get("last_price") is None:
                continue
            ohlc = record.get("ohlc") or {}
            out[original] = QuoteData(
                symbol=original,
                price=record["last_price"],
                source_timestamp=_parse_ist(
                    record.get("exchange_timestamp") or record.get("timestamp")
                ) or datetime.now(timezone.utc),
                provider=self.name,
                quality=DataQuality.LIVE,
                previous_close=ohlc.get("close"),
                day_open=ohlc.get("open"),
                day_high=ohlc.get("high"),
                day_low=ohlc.get("low"),
                volume=record.get("volume"),
                currency="INR",
            )
        return out

    # ------------------------------------------------------------ historical
    def fetch_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [
            Bar(
                symbol=symbol, trade_date=candle["timestamp"].date(),
                open=candle["open"], high=candle["high"], low=candle["low"],
                close=candle["close"], volume=int(candle["volume"] or 0),
                adj_close=None,
                provider=self.name, quality=DataQuality.EOD,
                source_timestamp=candle["timestamp"],
            )
            for candle in self._candles(symbol, start, end, "day")
        ]

    def fetch_intraday_bars(
        self, symbol: str, interval: str = "5m", lookback_days: int = 1
    ) -> list[IntradayBar]:
        kite_interval = INTERVAL_MAP.get(interval)
        if kite_interval is None:
            raise ValueError(
                f"unsupported interval {interval!r}; supported: {sorted(INTERVAL_MAP)}"
            )
        end = datetime.now(IST).date()
        start = end - timedelta(days=max(1, lookback_days))
        return [
            IntradayBar(
                symbol=symbol, bar_timestamp=candle["timestamp"], interval=interval,
                open=candle["open"], high=candle["high"], low=candle["low"],
                close=candle["close"], volume=int(candle["volume"] or 0),
                provider=self.name, quality=DataQuality.LIVE,
            )
            for candle in self._candles(symbol, start, end, kite_interval)
        ]

    def _candles(
        self, symbol: str, start: date, end: date, interval: str
    ) -> list[dict]:
        """Fetch candles, chunked to respect Kite's per-request span caps."""
        token = self.instrument_token(self.to_kite_symbol(symbol).split(":", 1)[1])
        span = MAX_SPAN_DAYS.get(interval, 200)

        candles: list[dict] = []
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=span - 1), end)
            try:
                data = self._get(
                    f"/instruments/historical/{token}/{interval}",
                    params={
                        "from": window_start.strftime("%Y-%m-%d"),
                        "to": window_end.strftime("%Y-%m-%d"),
                    },
                )
            except KiteSubscriptionError as exc:
                raise KiteSubscriptionError(
                    "historical candle data is a separate paid Kite add-on; "
                    f"the account does not have it ({exc})"
                ) from exc

            for row in data.get("candles") or []:
                # [timestamp, open, high, low, close, volume, (oi)]
                if len(row) < 6:
                    continue
                stamp = _parse_ist(row[0])
                if stamp is None:
                    continue
                candles.append(
                    {
                        "timestamp": stamp, "open": row[1], "high": row[2],
                        "low": row[3], "close": row[4], "volume": row[5],
                    }
                )
            window_start = window_end + timedelta(days=1)

        return candles

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int = 10) -> list[SecurityInfo]:
        table = self.load_instruments("NSE")
        needle = query.strip().upper()
        matches = [
            record for key, record in table.items()
            if needle in record["tradingsymbol"].upper()
            or needle in (record.get("name") or "").upper()
        ]
        matches.sort(key=lambda r: (r["tradingsymbol"] != needle, len(r["tradingsymbol"])))
        return [
            SecurityInfo(
                symbol=record["tradingsymbol"], name=record["name"],
                currency="INR", exchange_code=record["exchange"],
            )
            for record in matches[:limit]
        ]

    # ---------------------------------------------------------------- health
    def health_check(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "not configured (KITE_API_KEY / KITE_ACCESS_TOKEN)"
        try:
            profile = self._get("/user/profile")
            return True, f"authenticated as {profile.get('user_id', 'unknown')}"
        except KiteAuthError as exc:
            return False, f"authentication failed: {exc}"
        except Exception as exc:
            return False, str(exc)[:200]


def _parse_ist(value) -> datetime | None:
    """Parse a Kite timestamp into an aware UTC datetime.

    Kite returns exchange timestamps in IST. Some are ISO strings carrying an
    offset, some are naive, and the historical endpoint returns datetimes. A
    naive value is assumed IST -- reading it as UTC would misdate every bar by
    five and a half hours.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)
