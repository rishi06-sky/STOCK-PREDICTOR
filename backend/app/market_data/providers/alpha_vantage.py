"""Alpha Vantage adapter.

Free tier is roughly 25 requests per DAY, which makes this a fallback or
fundamentals source rather than a primary feed. Quotes are ~15 minutes delayed.
Exceeding the quota returns HTTP 200 with a "Note"/"Information" body, which is
detected explicitly below and surfaced as a rate-limit error.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.core.config import settings
from app.core.exceptions import ProviderError, ProviderRateLimited, SymbolNotFound
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider
from app.market_data.types import Bar, FundamentalsData, QuoteData
from app.models.enums import DataQuality

log = get_logger(__name__)

BASE_URL = "https://www.alphavantage.co/query"

# Alpha Vantage's Indian coverage is the BSE namespace ("RELIANCE.BSE"); it
# publishes no NSE equivalent. Mapping an NSE listing onto ".BSE" would quote a
# different order book under the security we asked about, so NSE is declined
# and the chain falls through to a provider that does cover it. Set an explicit
# per-security override in Security.provider_symbols to force a spelling.
SUFFIX_TO_ALPHA_VANTAGE = {".BO": ".BSE"}
UNSUPPORTED_SUFFIXES = (".NS",)


class AlphaVantageProvider(MarketDataProvider):
    name = "alpha_vantage"
    default_quality = DataQuality.DELAYED
    requires_key = True

    def __init__(self, api_key: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key or settings.alpha_vantage_api_key
        self.rate_limit_per_minute = settings.alpha_vantage_rate_limit_per_minute

    def is_configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def to_alpha_vantage_symbol(symbol: str) -> str:
        """Map our canonical spelling onto Alpha Vantage's."""
        upper = symbol.upper()
        for suffix, replacement in SUFFIX_TO_ALPHA_VANTAGE.items():
            if upper.endswith(suffix):
                return upper[: -len(suffix)] + replacement
        return upper

    def supports_symbol(self, symbol: str) -> bool:
        return not symbol.upper().endswith(UNSUPPORTED_SUFFIXES)

    def _require_supported(self, symbol: str) -> str:
        if not self.supports_symbol(symbol):
            raise ProviderError(
                self.name,
                f"{symbol}: Alpha Vantage publishes no NSE listings; "
                "a BSE symbol or an explicit override is required",
                retryable=False,
            )
        return self.to_alpha_vantage_symbol(symbol)

    def _call(self, params: dict) -> dict:
        if not self.is_configured():
            raise ProviderError(self.name, "no API key configured", retryable=False)
        response = self._request(
            "GET", BASE_URL, params={**params, "apikey": self.api_key}
        )
        payload = response.json() or {}
        # Quota and error conditions arrive as 200 OK with a prose body.
        if "Note" in payload or "Information" in payload:
            raise ProviderRateLimited(
                self.name, str(payload.get("Note") or payload.get("Information"))[:200]
            )
        if "Error Message" in payload:
            raise SymbolNotFound(self.name, str(params.get("symbol", "?")))
        return payload

    def fetch_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        vendor_symbol = self._require_supported(symbol)
        payload = self._call(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": vendor_symbol,
                "outputsize": "full" if (end - start).days > 100 else "compact",
            }
        )
        series = payload.get("Time Series (Daily)") or {}
        if not series:
            raise ProviderError(self.name, f"empty series for {symbol}")

        bars: list[Bar] = []
        for day_str, row in series.items():
            try:
                bar_day = datetime.strptime(day_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not start <= bar_day <= end:
                continue
            try:
                bars.append(
                    Bar(
                        symbol=symbol, trade_date=bar_day,
                        open=float(row["1. open"]), high=float(row["2. high"]),
                        low=float(row["3. low"]), close=float(row["4. close"]),
                        adj_close=float(row.get("5. adjusted close") or row["4. close"]),
                        volume=int(float(row.get("6. volume") or 0)),
                        provider=self.name, quality=DataQuality.EOD,
                        source_timestamp=datetime.combine(
                            bar_day, time(23, 59), tzinfo=timezone.utc
                        ),
                    )
                )
            except (KeyError, ValueError):
                continue
        return sorted(bars, key=lambda b: b.trade_date)

    def fetch_quote(self, symbol: str) -> QuoteData:
        vendor_symbol = self._require_supported(symbol)
        payload = self._call({"function": "GLOBAL_QUOTE", "symbol": vendor_symbol})
        quote = payload.get("Global Quote") or {}
        price = quote.get("05. price")
        if not price:
            raise SymbolNotFound(self.name, symbol)
        day_str = quote.get("07. latest trading day")
        source_ts = (
            datetime.combine(
                datetime.strptime(day_str, "%Y-%m-%d").date(), time(23, 59), tzinfo=timezone.utc
            )
            if day_str
            else datetime.now(timezone.utc)
        )
        return QuoteData(
            symbol=symbol, price=float(price), source_timestamp=source_ts,
            provider=self.name, quality=DataQuality.DELAYED,
            previous_close=_opt_float(quote.get("08. previous close")),
            day_open=_opt_float(quote.get("02. open")),
            day_high=_opt_float(quote.get("03. high")),
            day_low=_opt_float(quote.get("04. low")),
            volume=int(float(quote.get("06. volume") or 0)) or None,
        )

    def fetch_fundamentals(self, symbol: str) -> FundamentalsData:
        vendor_symbol = self._require_supported(symbol)
        payload = self._call({"function": "OVERVIEW", "symbol": vendor_symbol})
        if not payload.get("Symbol"):
            raise SymbolNotFound(self.name, symbol)
        return FundamentalsData(
            symbol=symbol, provider=self.name,
            as_of_date=datetime.now(timezone.utc).date(), period="TTM",
            pe_ratio=_opt_float(payload.get("PERatio")),
            pb_ratio=_opt_float(payload.get("PriceToBookRatio")),
            eps=_opt_float(payload.get("EPS")),
            revenue=_opt_float(payload.get("RevenueTTM")),
            revenue_growth=_opt_float(payload.get("QuarterlyRevenueGrowthYOY")),
            profit_growth=_opt_float(payload.get("QuarterlyEarningsGrowthYOY")),
            roe=_opt_float(payload.get("ReturnOnEquityTTM")),
            dividend_yield=_opt_float(payload.get("DividendYield")),
            gross_margin=_opt_float(payload.get("GrossProfitTTM")),
            operating_margin=_opt_float(payload.get("OperatingMarginTTM")),
            net_margin=_opt_float(payload.get("ProfitMargin")),
            market_cap=_opt_float(payload.get("MarketCapitalization")),
        )


def _opt_float(value):
    """Alpha Vantage writes missing numbers as "None" or "-"."""
    if value in (None, "", "None", "-", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
