"""Market-data provider contract.

Adapters translate one vendor's wire format into the shared DTOs. Anything a
provider genuinely cannot supply raises `NotSupported` -- it never returns an
invented value.
"""
from __future__ import annotations

import abc
from datetime import date, timedelta

import httpx
from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter,
)

from app.core.config import settings
from app.core.exceptions import (
    ProviderError, ProviderRateLimited, ProviderUnavailable, SymbolNotFound,
)
from app.core.logging import get_logger
from app.market_data.rate_limit import get_bucket
from app.market_data.types import (
    Bar, FundamentalsData, IntradayBar, NewsItem, QuoteData, SecurityInfo,
)

log = get_logger(__name__)


def _should_retry(exc: BaseException) -> bool:
    """Retry only provider errors that declare themselves retryable."""
    return isinstance(exc, ProviderError) and exc.retryable


class NotSupported(ProviderError):
    """This provider does not offer the requested dataset."""

    def __init__(self, provider: str, capability: str):
        super().__init__(provider, f"does not support {capability}", retryable=False)


class MarketDataProvider(abc.ABC):
    """Base adapter.

    Subclasses set `name`, `default_quality` and `requires_key`, and implement
    whichever `fetch_*` methods the vendor actually serves.
    """

    name: str = "base"
    #: Freshness of this provider's quotes. Declaring EOD here is what stops
    #: end-of-day data being presented as a live price downstream.
    default_quality: str = "EOD"
    requires_key: bool = False
    rate_limit_per_minute: int = 60

    def __init__(self, *, client: httpx.Client | None = None):
        self._client = client
        self._owns_client = client is None

    # ----------------------------------------------------------- http plumbing
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(settings.provider_timeout_seconds),
                headers={
                    "User-Agent": settings.provider_user_agent,
                    "Accept": "application/json, text/csv, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @retry(
        # Honour the exception's own `retryable` flag rather than matching on
        # type alone. A policy denial or a dead API key raises
        # ProviderUnavailable with retryable=False, and retrying it just burns
        # the backoff window per symbol for a request that can never succeed.
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(settings.provider_max_retries),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Throttled, retrying HTTP call with vendor-status interpretation."""
        bucket = get_bucket(self.name, self.rate_limit_per_minute)
        if not bucket.acquire(timeout=30):
            raise ProviderRateLimited(self.name, "local throttle timeout")

        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.ProxyError as exc:
            # Egress policy denial reads as a proxy failure, not a vendor outage.
            raise ProviderUnavailable(
                self.name, f"blocked by network policy: {exc}", retryable=False
            ) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(self.name, f"connection failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(self.name, f"timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"http error: {exc}") from exc

        if response.status_code == 429:
            raise ProviderRateLimited(self.name, "provider returned 429")
        if response.status_code in (401, 403):
            raise ProviderError(
                self.name,
                f"access denied ({response.status_code}) -- check API key or egress policy",
                retryable=False,
            )
        if response.status_code == 404:
            raise SymbolNotFound(self.name, url)
        if response.status_code >= 500:
            raise ProviderUnavailable(self.name, f"upstream {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(
                self.name, f"unexpected {response.status_code}", retryable=False
            )
        return response

    # -------------------------------------------------------------- capabilities
    @property
    def capabilities(self) -> set[str]:
        caps = set()
        for cap, method in (
            ("daily", "fetch_daily_bars"),
            ("quote", "fetch_quote"),
            ("intraday", "fetch_intraday_bars"),
            ("fundamentals", "fetch_fundamentals"),
            ("news", "fetch_news"),
            ("search", "search"),
        ):
            if getattr(type(self), method) is not getattr(MarketDataProvider, method):
                caps.add(cap)
        return caps

    def is_configured(self) -> bool:
        return True

    # ------------------------------------------------------------------ fetches
    def fetch_daily_bars(
        self, symbol: str, start: date, end: date
    ) -> list[Bar]:
        raise NotSupported(self.name, "daily bars")

    def fetch_quote(self, symbol: str) -> QuoteData:
        raise NotSupported(self.name, "quotes")

    def fetch_intraday_bars(
        self, symbol: str, interval: str = "5m", lookback_days: int = 1
    ) -> list[IntradayBar]:
        raise NotSupported(self.name, "intraday bars")

    def fetch_fundamentals(self, symbol: str) -> FundamentalsData:
        raise NotSupported(self.name, "fundamentals")

    def fetch_news(self, symbol: str | None = None, limit: int = 25) -> list[NewsItem]:
        raise NotSupported(self.name, "news")

    def search(self, query: str, limit: int = 10) -> list[SecurityInfo]:
        raise NotSupported(self.name, "symbol search")

    health_check_symbol: str = "AAPL"

    def health_check(self) -> tuple[bool, str]:
        """Cheap real probe. Returns (ok, detail) and never raises.

        Providers without a quote endpoint are probed with a short daily-bar
        request instead of being assumed reachable -- an unverified "healthy"
        is exactly the false claim the freshness guards exist to prevent.
        """
        if not self.is_configured():
            return False, "not configured (missing API key)"
        caps = self.capabilities
        try:
            if "quote" in caps:
                self.fetch_quote(self.health_check_symbol)
                return True, "ok (quote probe)"
            if "daily" in caps:
                end = date.today()
                bars = self.fetch_daily_bars(
                    self.health_check_symbol, end - timedelta(days=10), end
                )
                if not bars:
                    return False, "daily probe returned no bars"
                return True, f"ok (daily probe, {len(bars)} bars)"
            return False, "no probeable capability"
        except Exception as exc:
            return False, str(exc)
