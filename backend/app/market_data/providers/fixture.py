"""Replay provider for automated tests ONLY.

This is the single place in the codebase permitted to originate data that did
not come from a market. Everything it emits is tagged DataQuality.SYNTHETIC so
it is visible end to end, and construction is refused unless the process is
explicitly in test mode -- a production deployment cannot instantiate it even
if misconfigured.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import ProviderError, SymbolNotFound
from app.market_data.base import MarketDataProvider
from app.market_data.types import Bar, NewsItem, QuoteData
from app.models.enums import DataQuality


class FixtureProviderRefused(RuntimeError):
    """Raised when the fixture provider is requested outside of tests."""


class FixtureProvider(MarketDataProvider):
    name = "fixture"
    default_quality = DataQuality.SYNTHETIC
    requires_key = False

    def __init__(self, fixture_dir: str | Path | None = None, **kwargs):
        if not (settings.environment == "test" or settings.enable_fixture_provider):
            raise FixtureProviderRefused(
                "FixtureProvider requires ENVIRONMENT=test or "
                "ENABLE_FIXTURE_PROVIDER=true; it must never serve production."
            )
        if settings.is_production:
            raise FixtureProviderRefused("FixtureProvider is forbidden in production")
        super().__init__(**kwargs)
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.rate_limit_per_minute = 100_000

    # Deterministic pseudo-random walk. Seeded per symbol so a given symbol
    # always replays the identical series, which keeps tests reproducible.
    @staticmethod
    def _walk(symbol: str, days: int, *, base: float = 100.0) -> list[float]:
        seed = sum(ord(c) * (i + 1) for i, c in enumerate(symbol)) or 7
        prices, level = [], base + (seed % 400)
        for i in range(days):
            x = math.sin((seed + i * 13) * 0.37) + math.sin((seed + i * 7) * 0.11) * 0.6
            level = max(1.0, level * (1 + x * 0.012))
            prices.append(round(level, 2))
        return prices

    def fetch_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        if self.fixture_dir:
            recorded = self._from_disk(symbol, start, end)
            if recorded is not None:
                return recorded
        if start > end:
            raise ValueError("start must not be after end")

        days = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]
        closes = self._walk(symbol, len(days))
        bars: list[Bar] = []
        for day, close in zip(days, closes):
            spread = max(0.01, close * 0.011)
            o = round(close - spread * 0.4, 2)
            h = round(max(o, close) + spread * 0.5, 2)
            l = round(min(o, close) - spread * 0.5, 2)
            bars.append(
                Bar(
                    symbol=symbol, trade_date=day,
                    open=o, high=h, low=max(0.01, l), close=close,
                    adj_close=close,
                    volume=500_000 + (hash((symbol, day)) % 400_000),
                    provider=self.name, quality=DataQuality.SYNTHETIC,
                    source_timestamp=datetime.combine(day, time(23, 59), tzinfo=timezone.utc),
                )
            )
        return bars

    def _from_disk(self, symbol: str, start: date, end: date) -> list[Bar] | None:
        path = self.fixture_dir / f"{symbol.replace('/', '_')}.json"
        if not path.exists():
            return None
        rows = json.loads(path.read_text())
        bars = []
        for row in rows:
            day = date.fromisoformat(row["date"])
            if not start <= day <= end:
                continue
            bars.append(
                Bar(
                    symbol=symbol, trade_date=day,
                    open=row["open"], high=row["high"], low=row["low"],
                    close=row["close"], adj_close=row.get("adj_close"),
                    volume=int(row.get("volume", 0)),
                    provider=self.name, quality=DataQuality.SYNTHETIC,
                    source_timestamp=datetime.combine(day, time(23, 59), tzinfo=timezone.utc),
                )
            )
        return bars

    def fetch_quote(self, symbol: str) -> QuoteData:
        today = datetime.now(timezone.utc).date()
        bars = self.fetch_daily_bars(symbol, today - timedelta(days=10), today)
        if not bars:
            raise SymbolNotFound(self.name, symbol)
        last = bars[-1]
        return QuoteData(
            symbol=symbol, price=last.close,
            source_timestamp=datetime.now(timezone.utc),
            provider=self.name, quality=DataQuality.SYNTHETIC,
            previous_close=bars[-2].close if len(bars) > 1 else None,
            day_open=last.open, day_high=last.high, day_low=last.low,
            volume=last.volume,
        )

    def fetch_news(self, symbol: str | None = None, limit: int = 25) -> list[NewsItem]:
        now = datetime.now(timezone.utc)
        return [
            NewsItem(
                headline=f"[SYNTHETIC FIXTURE] Test headline {i} for {symbol or 'market'}",
                source="fixture", published_at=now - timedelta(hours=i),
                provider=self.name, symbol=symbol,
            )
            for i in range(min(limit, 5))
        ]
