"""Currency rules for portfolio valuation.

The platform stores no FX rates and has no provider that supplies them. Summing
positions quoted in different currencies therefore cannot be done correctly --
and the result would not look broken: adding dollars to rupees yields a
plausible number that is wrong by whatever the exchange rate happens to be.
That number is not cosmetic. It becomes portfolio equity, which sizes every
position and polices the exposure and drawdown limits, so a silent mix would
quietly loosen the risk controls it feeds.

Rather than invent a rate, the engines here treat a foreign-currency holding as
something they cannot express: entries into one are refused, and any that
already exist are excluded from the totals and reported instead of folded in.
Adding real FX support means an FX rate source plus a conversion at each
valuation, not a default rate.
"""
from __future__ import annotations


def normalise(code: str | None) -> str:
    """Canonical form of a currency code; unknown or missing becomes ""."""
    return (code or "").strip().upper()


def is_convertible(holding_currency: str | None, portfolio_currency: str | None) -> bool:
    """Whether a holding can be expressed in the portfolio's currency.

    With no FX rate source that is true only when the two match. A missing code
    on either side is not treated as a wildcard -- it is unknown, and an unknown
    currency is exactly the case that must not be summed.
    """
    holding = normalise(holding_currency)
    portfolio = normalise(portfolio_currency)
    if not holding or not portfolio:
        return False
    return holding == portfolio
