"""Yahoo Finance adapter.

Covers NSE (".NS"), BSE (".BO"), NYSE and NASDAQ from one endpoint with no API
key, which makes it the only zero-cost source that spans both the Indian and US
markets this platform targets.

Caveats, stated plainly:
  * This is an undocumented endpoint intended for Yahoo's own front end. It has
    no SLA and its shape can change without notice -- hence the adapter layer.
  * Intraday quotes are typically delayed 15 minutes or more, so every quote is
    tagged DELAYED, never LIVE. Daily bars are tagged EOD once the session has
    settled.
  * Review Yahoo's terms before commercial use.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.core.config import settings
from app.core.exceptions import ProviderError, SymbolNotFound
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider
from app.market_data.types import (
    Bar, FundamentalsData, IntradayBar, NewsItem, QuoteData, SecurityInfo,
)
from app.models.enums import DataQuality

log = get_logger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "1wk", "1mo"}


class YahooFinanceProvider(MarketDataProvider):
    name = "yahoo"
    default_quality = DataQuality.DELAYED
    requires_key = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rate_limit_per_minute = settings.yahoo_rate_limit_per_minute

    # ------------------------------------------------------------------ chart
    def _chart(self, symbol: str, *, params: dict) -> dict:
        response = self._request("GET", CHART_URL.format(symbol=symbol), params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, f"non-JSON chart response: {exc}") from exc

        chart = (payload or {}).get("chart") or {}
        if chart.get("error"):
            code = (chart["error"] or {}).get("code", "")
            if "NotFound" in str(code) or "Not Found" in str(code):
                raise SymbolNotFound(self.name, symbol)
            raise ProviderError(self.name, f"chart error: {chart['error']}")

        results = chart.get("result") or []
        if not results:
            raise SymbolNotFound(self.name, symbol)
        return results[0]

    @staticmethod
    def _series(result: dict) -> tuple[list, dict, list | None]:
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote_blocks = indicators.get("quote") or [{}]
        quote = quote_blocks[0] if quote_blocks else {}
        adj_blocks = indicators.get("adjclose") or []
        adj = (adj_blocks[0] or {}).get("adjclose") if adj_blocks else None
        return timestamps, quote, adj

    # ------------------------------------------------------------- daily bars
    def fetch_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        if start > end:
            raise ValueError("start must not be after end")
        result = self._chart(
            symbol,
            params={
                "period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
                # Yahoo's period2 is exclusive at day granularity; pad a day.
                "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()),
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            },
        )
        meta = result.get("meta") or {}
        tz_offset = meta.get("gmtoffset") or 0
        timestamps, quote, adj = self._series(result)

        bars: list[Bar] = []
        for i, ts in enumerate(timestamps):
            o, h, l, c, v = (
                _at(quote.get("open"), i), _at(quote.get("high"), i),
                _at(quote.get("low"), i), _at(quote.get("close"), i),
                _at(quote.get("volume"), i),
            )
            # Yahoo pads the series with nulls for halted or non-trading days.
            if None in (o, h, l, c):
                continue
            # Convert to the exchange's local calendar day so an Indian bar is
            # not filed under the previous UTC date.
            bar_day = datetime.fromtimestamp(ts + tz_offset, tz=timezone.utc).date()
            bars.append(
                Bar(
                    symbol=symbol,
                    trade_date=bar_day,
                    open=o, high=h, low=l, close=c,
                    volume=int(v or 0),
                    adj_close=_at(adj, i),
                    provider=self.name,
                    quality=DataQuality.EOD,
                    source_timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                )
            )
        return bars

    # ---------------------------------------------------------------- intraday
    def fetch_intraday_bars(
        self, symbol: str, interval: str = "5m", lookback_days: int = 1
    ) -> list[IntradayBar]:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"unsupported interval {interval!r}")
        result = self._chart(
            symbol,
            params={"range": f"{max(1, lookback_days)}d", "interval": interval},
        )
        timestamps, quote, _ = self._series(result)

        bars: list[IntradayBar] = []
        for i, ts in enumerate(timestamps):
            o, h, l, c, v = (
                _at(quote.get("open"), i), _at(quote.get("high"), i),
                _at(quote.get("low"), i), _at(quote.get("close"), i),
                _at(quote.get("volume"), i),
            )
            if None in (o, h, l, c):
                continue
            bars.append(
                IntradayBar(
                    symbol=symbol,
                    bar_timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                    interval=interval,
                    open=o, high=h, low=l, close=c,
                    volume=int(v or 0),
                    provider=self.name,
                    quality=DataQuality.DELAYED,
                )
            )
        return bars

    # ------------------------------------------------------------------ quote
    def fetch_quote(self, symbol: str) -> QuoteData:
        result = self._chart(symbol, params={"range": "1d", "interval": "1m"})
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            raise ProviderError(self.name, f"no regularMarketPrice for {symbol}")

        # Prefer the venue's own strike time. Falling back to "now" would make
        # stale data look fresh, so we only do it when Yahoo omits the field and
        # we mark the result no better than DELAYED regardless.
        struck = meta.get("regularMarketTime")
        source_ts = (
            datetime.fromtimestamp(struck, tz=timezone.utc)
            if struck
            else datetime.now(timezone.utc)
        )
        return QuoteData(
            symbol=symbol,
            price=price,
            source_timestamp=source_ts,
            provider=self.name,
            quality=DataQuality.DELAYED,
            previous_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
            day_open=meta.get("regularMarketOpen"),
            day_high=meta.get("regularMarketDayHigh"),
            day_low=meta.get("regularMarketDayLow"),
            volume=meta.get("regularMarketVolume"),
            currency=meta.get("currency"),
        )

    # ----------------------------------------------------------- fundamentals
    def fetch_fundamentals(self, symbol: str) -> FundamentalsData:
        modules = "defaultKeyStatistics,financialData,summaryDetail"
        response = self._request(
            "GET",
            QUOTE_SUMMARY_URL.format(symbol=symbol),
            params={"modules": modules},
        )
        payload = response.json() or {}
        results = ((payload.get("quoteSummary") or {}).get("result")) or []
        if not results:
            raise SymbolNotFound(self.name, symbol)
        blob = results[0]
        stats = blob.get("defaultKeyStatistics") or {}
        fin = blob.get("financialData") or {}
        summary = blob.get("summaryDetail") or {}

        return FundamentalsData(
            symbol=symbol,
            provider=self.name,
            as_of_date=datetime.now(timezone.utc).date(),
            period="TTM",
            pe_ratio=_raw(summary.get("trailingPE")),
            pb_ratio=_raw(stats.get("priceToBook")),
            eps=_raw(stats.get("trailingEps")),
            revenue=_raw(fin.get("totalRevenue")),
            revenue_growth=_raw(fin.get("revenueGrowth")),
            net_income=_raw(stats.get("netIncomeToCommon")),
            roe=_raw(fin.get("returnOnEquity")),
            debt_to_equity=_ratio_pct(_raw(fin.get("debtToEquity"))),
            free_cash_flow=_raw(fin.get("freeCashflow")),
            dividend_yield=_raw(summary.get("dividendYield")),
            gross_margin=_raw(fin.get("grossMargins")),
            operating_margin=_raw(fin.get("operatingMargins")),
            net_margin=_raw(fin.get("profitMargins")),
            earnings_growth=_raw(fin.get("earningsGrowth")),
            market_cap=_raw(summary.get("marketCap")),
            raw={"modules": modules},
        )

    # ----------------------------------------------------------------- search
    def search(self, query: str, limit: int = 10) -> list[SecurityInfo]:
        response = self._request(
            "GET", SEARCH_URL,
            params={"q": query, "quotesCount": limit, "newsCount": 0},
        )
        quotes = (response.json() or {}).get("quotes") or []
        out: list[SecurityInfo] = []
        for q in quotes[:limit]:
            if not q.get("symbol"):
                continue
            out.append(
                SecurityInfo(
                    symbol=q["symbol"],
                    name=q.get("longname") or q.get("shortname") or q["symbol"],
                    currency=q.get("currency") or "USD",
                    exchange_code=q.get("exchange"),
                    sector=q.get("sector"),
                    industry=q.get("industry"),
                )
            )
        return out

    def fetch_news(self, symbol: str | None = None, limit: int = 25) -> list[NewsItem]:
        response = self._request(
            "GET", SEARCH_URL,
            params={"q": symbol or "stock market", "quotesCount": 0, "newsCount": limit},
        )
        items = (response.json() or {}).get("news") or []
        out: list[NewsItem] = []
        for item in items[:limit]:
            published = item.get("providerPublishTime")
            if not published:
                continue
            out.append(
                NewsItem(
                    headline=item.get("title") or "",
                    source=item.get("publisher") or "Yahoo Finance",
                    published_at=datetime.fromtimestamp(published, tz=timezone.utc),
                    provider=self.name,
                    url=item.get("link"),
                    symbol=symbol,
                    category=item.get("type"),
                )
            )
        return [i for i in out if i.headline]


def _at(series: list | None, index: int):
    if not series or index >= len(series):
        return None
    return series[index]


def _raw(node):
    """Yahoo wraps numbers as {"raw": 1.23, "fmt": "1.23"}; unwrap defensively."""
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("raw")
    if isinstance(node, (int, float)):
        return node
    return None


def _ratio_pct(value):
    """Yahoo reports debt/equity as a percentage; normalise to a ratio."""
    return None if value is None else value / 100.0
