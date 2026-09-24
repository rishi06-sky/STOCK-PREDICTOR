"""A portfolio total may only sum values it can express in one currency.

The platform stores no FX rates. Adding a dollar-priced position to a rupee
portfolio does not raise -- it yields a plausible number that is wrong by
whatever the exchange rate happens to be. That number becomes portfolio equity,
which sizes every position and enforces the exposure and drawdown limits, so a
silent mix quietly loosens the controls it feeds.

These tests pin the two places that aggregate (PortfolioEngine.value and
RiskEngine.portfolio_equity) and the entry check that stops the mismatch
arising in the first place.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.money import is_convertible, normalise
from app.core.security import hash_password
from app.models.enums import DataQuality, PositionStatus, UserRole
from app.models.market import Quote, Security
from app.models.trading import Holding, Portfolio
from app.models.platform import User
from app.portfolio.engine import PortfolioEngine
from app.risk.engine import RiskEngine


@pytest.fixture
def mixed_currency_portfolio(seeded_db):
    """A rupee portfolio holding one INR security and one priced in USD.

    The USD listing is fabricated locally rather than seeded: the shipped
    universe is India-only, which is exactly why this bug stayed latent.
    """
    user = User(
        email="fx@example.com", password_hash=hash_password("a-strong-test-passphrase"),
        role=UserRole.ADMIN,
    )
    seeded_db.add(user)
    seeded_db.flush()

    inr = seeded_db.scalars(select(Security).where(Security.symbol == "RELIANCE")).first()
    usd = Security(
        exchange_id=inr.exchange_id, symbol="ACME", name="Acme Corp",
        currency="USD", sector="Technology",
    )
    seeded_db.add(usd)
    seeded_db.flush()

    now = datetime.now(timezone.utc)
    for security, price in ((inr, 1000.0), (usd, 100.0)):
        seeded_db.add(
            Quote(
                security_id=security.id, price=price, previous_close=price,
                provider="test", quality=DataQuality.SYNTHETIC, source_timestamp=now,
                ingested_at=now,
            )
        )

    portfolio = Portfolio(
        user_id=user.id, name="FX Test", mode="PAPER", currency="INR",
        starting_cash=1_000_000, cash=500_000, peak_equity=1_000_000,
    )
    seeded_db.add(portfolio)
    seeded_db.flush()

    for security in (inr, usd):
        seeded_db.add(
            Holding(
                portfolio_id=portfolio.id, security_id=security.id, quantity=100,
                average_cost=100.0, status=PositionStatus.OPEN, opened_at=now,
            )
        )
    seeded_db.flush()
    return seeded_db, portfolio, inr, usd


class TestConvertibility:
    def test_same_currency_is_convertible(self):
        assert is_convertible("INR", "INR")
        assert is_convertible(" inr ", "INR")  # normalised

    def test_different_currencies_are_not(self):
        assert not is_convertible("USD", "INR")

    def test_a_missing_code_is_unknown_not_a_wildcard(self):
        """An absent currency is the case that most needs excluding."""
        assert not is_convertible(None, "INR")
        assert not is_convertible("INR", None)
        assert not is_convertible("", "")

    def test_normalise(self):
        assert normalise(" usd ") == "USD"
        assert normalise(None) == ""


class TestPortfolioValuation:
    def test_a_foreign_position_is_left_out_of_the_totals(self, mixed_currency_portfolio):
        """Regression guard for the defect.

        Before this, positions_value was 100*1000 + 100*100 = 110,000: rupees
        and dollars added together. Only the rupee leg belongs in the total.
        """
        db, portfolio, _, _ = mixed_currency_portfolio
        view = PortfolioEngine(db).value(portfolio)
        assert view.positions_value == pytest.approx(100_000.0)  # not 110,000
        assert view.equity == pytest.approx(500_000 + 100_000)

    def test_the_excluded_position_is_still_reported(self, mixed_currency_portfolio):
        """Excluding it from the arithmetic must not hide it from the user."""
        db, portfolio, _, _ = mixed_currency_portfolio
        view = PortfolioEngine(db).value(portfolio)
        acme = next(p for p in view.positions if p.symbol == "ACME")
        assert acme.counted_in_totals is False
        assert acme.currency == "USD"
        assert acme.market_value == pytest.approx(10_000.0)  # in USD, its own currency

    def test_the_mismatch_is_warned_about(self, mixed_currency_portfolio):
        db, portfolio, _, _ = mixed_currency_portfolio
        view = PortfolioEngine(db).value(portfolio)
        warning = next((w for w in view.warnings if "ACME" in w), None)
        assert warning is not None
        assert "USD" in warning and "INR" in warning

    def test_an_excluded_position_has_no_weight(self, mixed_currency_portfolio):
        """Its value was kept out of equity, so a share of equity is meaningless."""
        db, portfolio, _, _ = mixed_currency_portfolio
        view = PortfolioEngine(db).value(portfolio)
        acme = next(p for p in view.positions if p.symbol == "ACME")
        assert acme.weight == 0.0

    def test_equity_still_reconciles(self, mixed_currency_portfolio):
        db, portfolio, _, _ = mixed_currency_portfolio
        view = PortfolioEngine(db).value(portfolio)
        assert view.equity == pytest.approx(view.cash + view.positions_value, abs=0.01)

    def test_a_single_currency_portfolio_is_unaffected(self, mixed_currency_portfolio):
        """The common case must behave exactly as before."""
        db, portfolio, inr, usd = mixed_currency_portfolio
        db.execute(
            select(Holding).where(Holding.security_id == usd.id)
        )
        for holding in db.scalars(
            select(Holding).where(Holding.security_id == usd.id)
        ).all():
            holding.status = PositionStatus.CLOSED
        db.flush()
        view = PortfolioEngine(db).value(portfolio)
        assert view.positions_value == pytest.approx(100_000.0)
        assert all(p.counted_in_totals for p in view.positions)
        assert not [w for w in view.warnings if "no FX rate" in w]


class TestRiskEngine:
    def test_equity_excludes_a_foreign_holding(self, mixed_currency_portfolio):
        """This equity sizes positions, so erring low is the safe direction."""
        db, portfolio, _, _ = mixed_currency_portfolio
        equity, positions_value = RiskEngine(db).portfolio_equity(portfolio)
        assert positions_value == pytest.approx(100_000.0)  # not 110,000
        assert equity == pytest.approx(600_000.0)

    def test_entry_into_a_foreign_security_is_refused(self, mixed_currency_portfolio):
        """Prevent the mismatch rather than cope with it at valuation time."""
        db, portfolio, _, usd = mixed_currency_portfolio
        decision = RiskEngine(db).evaluate_entry(portfolio, usd, price=100.0, confidence=0.9)
        assert not decision.approved
        assert any("USD" in v and "INR" in v for v in decision.violations)

    def test_entry_into_a_matching_security_still_works(self, mixed_currency_portfolio):
        """The guard must not block the ordinary single-currency path."""
        db, portfolio, inr, _ = mixed_currency_portfolio
        decision = RiskEngine(db).evaluate_entry(portfolio, inr, price=1000.0, confidence=0.9)
        assert not any("FX rate" in v for v in decision.violations)

    def test_a_security_with_no_currency_is_refused(self, mixed_currency_portfolio):
        db, portfolio, inr, _ = mixed_currency_portfolio
        unknown = Security(
            exchange_id=inr.exchange_id, symbol="NOCUR", name="No Currency", currency="",
        )
        db.add(unknown)
        db.flush()
        decision = RiskEngine(db).evaluate_entry(portfolio, unknown, price=50.0, confidence=0.9)
        assert not decision.approved
        assert any("FX rate" in v for v in decision.violations)
