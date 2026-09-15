"""Symbol mapping is a correctness boundary, not a formatting detail.

Tickers collide across exchanges. "RELIANCE" is an NSE listing in INR to us,
but a bare ticker means a US listing to Stooq, and Alpha Vantage resolves many
bare Indian tickers to their NYSE ADR in USD. Handing a provider the wrong
spelling does not raise -- it returns a plausible series for a different
company in a different currency, which is then stored against our security.

These tests pin the one canonical spelling (exchange-suffixed, "RELIANCE.NS")
and each provider's translation away from it.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.market_data.providers.alpha_vantage import AlphaVantageProvider
from app.market_data.providers.finnhub import FinnhubProvider
from app.market_data.providers.kite import KiteConnectProvider
from app.market_data.providers.stooq import StooqProvider
from app.models.market import Exchange, Security

PROVIDERS = ["yahoo", "stooq", "alpha_vantage", "kite", "finnhub", "fixture"]


def _security(symbol="RELIANCE", suffix=".NS", overrides=None):
    security = Security(symbol=symbol, name="Test Listing", currency="INR")
    security.exchange = Exchange(
        code="NSE", name="National Stock Exchange",
        open_time="09:15", close_time="15:30", exchange_suffix=suffix,
    )
    security.provider_symbols = overrides
    return security


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_exchange_suffix_applies_to_every_provider(provider):
    """The old code suffixed for Yahoo only; everyone else got a bare ticker."""
    assert _security().provider_symbol(provider) == "RELIANCE.NS"


def test_a_bare_indian_ticker_would_reach_stooq_as_a_us_listing():
    """Regression guard for the actual defect.

    Stooq treats a ticker with no dot as American. This asserts the hazard
    exists, so the mapping below is demonstrably load-bearing rather than
    cosmetic.
    """
    assert StooqProvider.to_stooq_symbol("RELIANCE") == "reliance.us"


def test_stooq_receives_the_india_namespace():
    security = _security()
    assert StooqProvider.to_stooq_symbol(security.provider_symbol("stooq")) == "reliance.in"


def test_bse_also_maps_into_the_stooq_india_namespace():
    security = _security(suffix=".BO")
    assert StooqProvider.to_stooq_symbol(security.provider_symbol("stooq")) == "reliance.in"


def test_stooq_does_not_label_an_index_as_a_us_listing():
    """"^NSEI" has no dot, and Stooq reads dotless symbols as American."""
    index = _security(symbol="^NSEI", overrides={"*": "^NSEI"})
    assert StooqProvider.to_stooq_symbol(index.provider_symbol("stooq")) == "^nsei"


def test_kite_receives_its_exchange_prefixed_form():
    security = _security()
    assert KiteConnectProvider.to_kite_symbol(security.provider_symbol("kite")) == "NSE:RELIANCE"


def test_alpha_vantage_maps_bse_onto_its_own_suffix():
    security = _security(suffix=".BO")
    psym = security.provider_symbol("alpha_vantage")
    assert AlphaVantageProvider.to_alpha_vantage_symbol(psym) == "RELIANCE.BSE"


def test_alpha_vantage_declines_nse_rather_than_quoting_another_market():
    """Alpha Vantage publishes BSE but not NSE.

    Rewriting ".NS" to ".BSE" would answer with a different order book under
    the security we asked about, so the provider declines and the chain falls
    through instead.
    """
    provider = AlphaVantageProvider(api_key="test-key")
    assert provider.supports_symbol("RELIANCE.NS") is False
    assert provider.supports_symbol("RELIANCE.BO") is True


def test_finnhub_still_declines_non_us_listings():
    provider = FinnhubProvider(api_key="test-key")
    assert provider.supports_symbol("RELIANCE.NS") is False
    assert provider.supports_symbol("AAPL") is True


def test_an_explicit_per_provider_override_wins():
    security = _security(overrides={"alpha_vantage": "RELIANCE.BSE"})
    assert security.provider_symbol("alpha_vantage") == "RELIANCE.BSE"
    # Other providers keep the canonical spelling.
    assert security.provider_symbol("stooq") == "RELIANCE.NS"


def test_a_wildcard_override_pins_the_spelling_for_every_provider():
    """Index tickers carry their own prefix and must never be suffixed."""
    index = _security(symbol="^NSEI", overrides={"*": "^NSEI"})
    for provider in PROVIDERS:
        assert index.provider_symbol(provider) == "^NSEI"


def test_a_security_on_an_exchange_without_a_suffix_keeps_its_bare_symbol():
    assert _security(symbol="AAPL", suffix=None).provider_symbol("yahoo") == "AAPL"


def test_ingest_skips_a_provider_that_declines_the_symbol():
    """A provider's refusal has to be honoured by the caller.

    Before this, only Finnhub self-checked inside each fetch method; the
    ingester called providers blind. A provider that declines must be stepped
    over so the chain reaches one that does cover the listing.
    """
    from app.market_data.ingest import IngestionService

    class Declines:
        name = "declines"
        capabilities = {"daily"}
        asked: list[str] = []

        def is_configured(self):
            return True

        def supports_symbol(self, symbol):
            return False

        def fetch_daily_bars(self, symbol, start, end):  # pragma: no cover
            raise AssertionError("must not be called for a declined symbol")

    class Covers:
        name = "covers"
        capabilities = {"daily"}

        def is_configured(self):
            return True

        def supports_symbol(self, symbol):
            return True

        def fetch_daily_bars(self, symbol, start, end):
            Covers.seen = symbol
            return ["a-bar"]

    class Chain:
        providers = [Declines(), Covers()]

    ingestor = IngestionService(db=None, chain=Chain())
    bars, name = ingestor._fetch_daily_with_symbols(
        _security(), date(2026, 1, 1), date(2026, 2, 1)
    )
    assert name == "covers"
    assert bars == ["a-bar"]
    # The surviving provider still gets the canonical spelling.
    assert Covers.seen == "RELIANCE.NS"
