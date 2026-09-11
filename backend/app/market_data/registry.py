"""Provider registry and failover chain.

Callers ask the chain for data, not a specific vendor. The chain tries each
configured provider in order and only gives up when every one has failed --
at which point it raises rather than returning a stale or invented value.
"""
from __future__ import annotations

import threading
from datetime import date

from app.core.config import settings
from app.core.exceptions import AllProvidersFailed, ProviderError
from app.core.logging import get_logger
from app.market_data.base import MarketDataProvider, NotSupported
from app.market_data.providers.alpha_vantage import AlphaVantageProvider
from app.market_data.providers.finnhub import FinnhubProvider
from app.market_data.providers.stooq import StooqProvider
from app.market_data.providers.yahoo import YahooFinanceProvider
from app.market_data.types import (
    Bar, FundamentalsData, IntradayBar, NewsItem, QuoteData, SecurityInfo,
)

log = get_logger(__name__)

PROVIDER_CLASSES: dict[str, type[MarketDataProvider]] = {
    "yahoo": YahooFinanceProvider,
    "stooq": StooqProvider,
    "alpha_vantage": AlphaVantageProvider,
    "finnhub": FinnhubProvider,
}


def _fixture_class() -> type[MarketDataProvider]:
    from app.market_data.providers.fixture import FixtureProvider
    return FixtureProvider


class ProviderChain:
    """Ordered set of adapters with per-capability failover."""

    def __init__(self, providers: list[MarketDataProvider] | None = None):
        self._providers = providers if providers is not None else _build_default()
        self._lock = threading.Lock()

    @property
    def providers(self) -> list[MarketDataProvider]:
        return list(self._providers)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._providers]

    def get(self, name: str) -> MarketDataProvider | None:
        return next((p for p in self._providers if p.name == name), None)

    def _candidates(self, capability: str) -> list[MarketDataProvider]:
        return [
            p for p in self._providers
            if capability in p.capabilities and p.is_configured()
        ]

    def _try_each(self, capability: str, label: str, call):
        candidates = self._candidates(capability)
        if not candidates:
            raise AllProvidersFailed(
                label, {"chain": f"no configured provider offers '{capability}'"}
            )
        errors: dict[str, str] = {}
        for provider in candidates:
            try:
                result = call(provider)
            except NotSupported as exc:
                errors[provider.name] = str(exc)
                continue
            except ProviderError as exc:
                errors[provider.name] = str(exc)
                log.warning(
                    "provider_failed", provider=provider.name,
                    capability=capability, target=label, error=str(exc),
                )
                continue
            except Exception as exc:  # defensive: a parser bug must not halt the chain
                errors[provider.name] = f"unexpected: {exc}"
                log.error(
                    "provider_unexpected_error", provider=provider.name,
                    capability=capability, target=label, error=str(exc),
                )
                continue

            # An empty result is a miss, not a success -- keep trying.
            if result is None or (isinstance(result, list) and not result):
                errors[provider.name] = "returned no data"
                continue
            return result, provider.name

        raise AllProvidersFailed(label, errors)

    # -------------------------------------------------------------- accessors
    def get_daily_bars(
        self, symbol: str, start: date, end: date
    ) -> tuple[list[Bar], str]:
        return self._try_each(
            "daily", symbol, lambda p: p.fetch_daily_bars(symbol, start, end)
        )

    def get_quote(self, symbol: str) -> tuple[QuoteData, str]:
        return self._try_each("quote", symbol, lambda p: p.fetch_quote(symbol))

    def get_intraday_bars(
        self, symbol: str, interval: str = "5m", lookback_days: int = 1
    ) -> tuple[list[IntradayBar], str]:
        return self._try_each(
            "intraday", symbol,
            lambda p: p.fetch_intraday_bars(symbol, interval, lookback_days),
        )

    def get_fundamentals(self, symbol: str) -> tuple[FundamentalsData, str]:
        return self._try_each(
            "fundamentals", symbol, lambda p: p.fetch_fundamentals(symbol)
        )

    def get_news(self, symbol: str | None = None, limit: int = 25) -> tuple[list[NewsItem], str]:
        return self._try_each(
            "news", symbol or "market", lambda p: p.fetch_news(symbol, limit)
        )

    def search(self, query: str, limit: int = 10) -> tuple[list[SecurityInfo], str]:
        return self._try_each("search", query, lambda p: p.search(query, limit))

    def health(self) -> dict[str, dict]:
        report: dict[str, dict] = {}
        for provider in self._providers:
            ok, detail = provider.health_check()
            report[provider.name] = {
                "healthy": ok,
                "detail": detail,
                "configured": provider.is_configured(),
                "capabilities": sorted(provider.capabilities),
                "quality": str(provider.default_quality),
            }
        return report

    def close(self) -> None:
        for provider in self._providers:
            provider.close()


def _build_default() -> list[MarketDataProvider]:
    providers: list[MarketDataProvider] = []
    for name in settings.provider_chain:
        if name == "fixture":
            # Guarded by FixtureProvider's own constructor; see that module.
            providers.append(_fixture_class()())
            continue
        cls = PROVIDER_CLASSES.get(name)
        if cls is None:
            log.warning("unknown_provider_in_chain", provider=name)
            continue
        instance = cls()
        if instance.requires_key and not instance.is_configured():
            log.info("provider_skipped_no_key", provider=name)
            continue
        providers.append(instance)
    if not providers:
        log.error("empty_provider_chain", configured=settings.market_data_providers)
    return providers


_chain: ProviderChain | None = None
_chain_lock = threading.Lock()


def get_chain() -> ProviderChain:
    global _chain
    with _chain_lock:
        if _chain is None:
            _chain = ProviderChain()
        return _chain


def reset_chain() -> None:
    """Used by tests to rebuild the chain after changing configuration."""
    global _chain
    with _chain_lock:
        if _chain is not None:
            _chain.close()
        _chain = None
