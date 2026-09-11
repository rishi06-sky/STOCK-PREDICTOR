"""Stooq adapter -- free end-of-day CSV, no API key.

Stooq serves settled daily bars only. Every record is therefore tagged EOD and
this provider deliberately implements no quote endpoint: serving yesterday's
close as a "quote" is exactly the substitution this platform forbids.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timezone

from app.core.config import settings
from app.core.exceptions import ProviderError, SymbolNotFound
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider
from app.market_data.types import Bar
from app.models.enums import DataQuality

log = get_logger(__name__)

DAILY_URL = "https://stooq.com/q/d/l/"


class StooqProvider(MarketDataProvider):
    name = "stooq"
    default_quality = DataQuality.EOD
    requires_key = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rate_limit_per_minute = settings.stooq_rate_limit_per_minute

    @staticmethod
    def to_stooq_symbol(symbol: str) -> str:
        """Map our symbol conventions onto Stooq's."""
        s = symbol.lower()
        if s.endswith(".ns"):
            return s[:-3] + ".in"   # NSE
        if s.endswith(".bo"):
            return s[:-3] + ".in"   # BSE shares Stooq's India namespace
        if "." not in s:
            return s + ".us"        # bare tickers are US listings
        return s

    def fetch_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        response = self._request(
            "GET", DAILY_URL,
            params={
                "s": self.to_stooq_symbol(symbol),
                "i": "d",
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
            },
        )
        text = response.text.strip()
        # Stooq answers unknown symbols with a 200 and a plain-text message.
        if not text or text.lower().startswith("no data") or "exceeded" in text.lower():
            if "exceeded" in text.lower():
                raise ProviderError(self.name, "daily download limit exceeded")
            raise SymbolNotFound(self.name, symbol)

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "Close" not in reader.fieldnames:
            raise ProviderError(self.name, f"unexpected CSV header: {reader.fieldnames}")

        bars: list[Bar] = []
        for row in reader:
            try:
                bar_day = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                o, h, l, c = (
                    float(row["Open"]), float(row["High"]),
                    float(row["Low"]), float(row["Close"]),
                )
            except (KeyError, ValueError):
                continue  # skip malformed rows rather than inventing values
            if not start <= bar_day <= end:
                continue
            bars.append(
                Bar(
                    symbol=symbol, trade_date=bar_day,
                    open=o, high=h, low=l, close=c,
                    volume=int(float(row.get("Volume") or 0)),
                    adj_close=None,  # Stooq's free CSV is not split-adjusted
                    provider=self.name,
                    quality=DataQuality.EOD,
                    source_timestamp=datetime.combine(bar_day, time(23, 59), tzinfo=timezone.utc),
                )
            )
        return bars
