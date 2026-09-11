"""Finnhub adapter -- US quotes and company news.

Free tier allows ~60 calls/minute but covers US listings only and delays
quotes by roughly 20 minutes; Indian equities are not available without a paid
plan, so this provider declines non-US symbols rather than returning a wrong
instrument that happens to share a ticker.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.core.config import settings
from app.core.exceptions import ProviderError, SymbolNotFound
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider
from app.market_data.types import NewsItem, QuoteData
from app.models.enums import DataQuality

log = get_logger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
# Suffixes that denote a non-US listing on our symbol convention.
NON_US_SUFFIXES = (".NS", ".BO", ".L", ".TO", ".HK", ".AX", ".DE", ".PA")


class FinnhubProvider(MarketDataProvider):
    name = "finnhub"
    default_quality = DataQuality.DELAYED
    requires_key = True

    def __init__(self, api_key: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key or settings.finnhub_api_key
        self.rate_limit_per_minute = settings.finnhub_rate_limit_per_minute

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def supports_symbol(self, symbol: str) -> bool:
        return not symbol.upper().endswith(NON_US_SUFFIXES)

    def _call(self, path: str, params: dict) -> dict | list:
        if not self.is_configured():
            raise ProviderError(self.name, "no API key configured", retryable=False)
        response = self._request(
            "GET", f"{BASE_URL}{path}", params={**params, "token": self.api_key}
        )
        return response.json()

    def fetch_quote(self, symbol: str) -> QuoteData:
        if not self.supports_symbol(symbol):
            raise ProviderError(
                self.name, f"{symbol} is not a US listing; not covered on this plan",
                retryable=False,
            )
        payload = self._call("/quote", {"symbol": symbol}) or {}
        price = payload.get("c")
        # Finnhub returns zeros (not an error) for unknown symbols.
        if not price:
            raise SymbolNotFound(self.name, symbol)
        struck = payload.get("t")
        return QuoteData(
            symbol=symbol, price=price,
            source_timestamp=(
                datetime.fromtimestamp(struck, tz=timezone.utc)
                if struck else datetime.now(timezone.utc)
            ),
            provider=self.name, quality=DataQuality.DELAYED,
            previous_close=payload.get("pc") or None,
            day_open=payload.get("o") or None,
            day_high=payload.get("h") or None,
            day_low=payload.get("l") or None,
        )

    def fetch_news(self, symbol: str | None = None, limit: int = 25) -> list[NewsItem]:
        if symbol:
            today = datetime.now(timezone.utc).date()
            payload = self._call(
                "/company-news",
                {
                    "symbol": symbol,
                    "from": (today.replace(day=1)).isoformat(),
                    "to": today.isoformat(),
                },
            )
        else:
            payload = self._call("/news", {"category": "general"})

        items = payload if isinstance(payload, list) else []
        out: list[NewsItem] = []
        for item in items[:limit]:
            published = item.get("datetime")
            headline = item.get("headline")
            if not published or not headline:
                continue
            out.append(
                NewsItem(
                    headline=headline,
                    source=item.get("source") or "Finnhub",
                    published_at=datetime.fromtimestamp(published, tz=timezone.utc),
                    provider=self.name,
                    summary=item.get("summary") or None,
                    url=item.get("url") or None,
                    symbol=symbol,
                    category=item.get("category"),
                )
            )
        return out
