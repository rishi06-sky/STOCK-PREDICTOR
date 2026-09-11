"""Order routing, risk vetoes, fail-safe behaviour and the HTTP surface."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import DuplicateOrderError, KillSwitchEngaged, UnknownPriceError
from app.market_data.ingest import IngestionService
from app.models.enums import DataQuality, PositionStatus, UserRole
from app.models.market import Quote, Security
from app.models.platform import User
from app.models.trading import Holding, Order, Portfolio
from app.portfolio.engine import PortfolioEngine
from app.trading.brokers.live_guard import live_trading_status
from app.trading.order_manager import OrderManager
from app.trading.paper_engine import get_or_create_paper_portfolio
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


@pytest.fixture
def trading_setup(seeded_db):
    """A funded paper portfolio with priced, history-bearing securities."""
    from app.core.security import hash_password

    user = User(
        email="tester@example.com", password_hash=hash_password("a-strong-test-passphrase"),
        role=UserRole.ADMIN,
    )
    seeded_db.add(user)
    seeded_db.flush()

    securities = list(
        seeded_db.scalars(select(Security).where(Security.symbol.in_(["RELIANCE", "TCS", "AAPL"])))
    )
    service = IngestionService(seeded_db)
    for security in securities:
        service.ingest_daily(security, date(2025, 1, 1), date(2026, 1, 1), commit=False)
        service.ingest_quote(security, commit=False)
    seeded_db.flush()

    portfolio = Portfolio(
        user_id=user.id, name="Test Paper", mode="PAPER", currency="USD",
        starting_cash=1_000_000, cash=1_000_000, peak_equity=1_000_000,
    )
    seeded_db.add(portfolio)
    seeded_db.flush()
    return seeded_db, user, portfolio, securities


class TestOrderRouting:
    def test_a_position_can_be_opened_and_closed(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        manager = OrderManager(db)
        security = securities[0]

        opened = manager.open_position(portfolio, security, confidence=0.8, commit=False)
        assert opened.executed
        assert opened.quantity > 0

        db.flush()
        holding = db.scalars(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id, Holding.status == PositionStatus.OPEN
            )
        ).first()
        assert holding is not None

        closed = manager.close_position(portfolio, holding, commit=False)
        assert closed.executed
        assert holding.status is PositionStatus.CLOSED

    def test_fill_price_includes_adverse_slippage(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        security = securities[0]
        quote = db.get(Quote, security.id)
        reference = float(quote.price)

        report = OrderManager(db).open_position(
            portfolio, security, confidence=0.8, commit=False
        )
        expected = reference * (1 + settings.slippage_bps / 10_000)
        assert report.price == pytest.approx(expected, rel=1e-9)

    def test_cash_decreases_by_notional_plus_commission(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        before = float(portfolio.cash)
        report = OrderManager(db).open_position(
            portfolio, securities[0], confidence=0.8, commit=False
        )
        notional = report.quantity * report.price
        commission = notional * settings.commission_bps / 10_000
        assert float(portfolio.cash) == pytest.approx(before - notional - commission, rel=1e-6)

    def test_duplicate_orders_are_refused(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        manager = OrderManager(db)
        manager.open_position(portfolio, securities[0], confidence=0.8, commit=False)
        with pytest.raises(DuplicateOrderError):
            manager.open_position(portfolio, securities[0], confidence=0.8, commit=False)

    def test_only_one_order_row_survives_a_duplicate_attempt(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        manager = OrderManager(db)
        manager.open_position(portfolio, securities[0], confidence=0.8, commit=False)
        try:
            manager.open_position(portfolio, securities[0], confidence=0.8, commit=False)
        except DuplicateOrderError:
            pass
        assert db.scalar(select(func.count()).select_from(Order)) == 1


class TestFailSafeBehaviour:
    def test_no_quote_means_no_trade(self, trading_setup):
        db, _, portfolio, _ = trading_setup
        unpriced = db.scalars(
            select(Security).where(Security.symbol == "WIPRO").limit(1)
        ).first()
        with pytest.raises(UnknownPriceError):
            OrderManager(db).open_position(portfolio, unpriced, confidence=0.9, commit=False)

    def test_a_stale_price_is_refused(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        security = securities[0]
        quote = db.get(Quote, security.id)
        # Age the quote past the halt threshold and mark it as real data.
        quote.source_timestamp = datetime.now(timezone.utc) - timedelta(days=3)
        quote.quality = DataQuality.EOD
        db.flush()
        with pytest.raises(UnknownPriceError, match="old"):
            OrderManager(db).open_position(portfolio, security, confidence=0.9, commit=False)

    def test_the_kill_switch_blocks_every_order(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        settings.kill_switch_engaged = True
        try:
            with pytest.raises(KillSwitchEngaged):
                OrderManager(db).open_position(
                    portfolio, securities[0], confidence=0.9, commit=False
                )
        finally:
            settings.kill_switch_engaged = False

    def test_live_trading_is_disabled_by_default(self):
        status = live_trading_status(broker_connected=False)
        assert not status.allowed
        assert len(status.blockers) >= 2


class TestRiskVetoes:
    def test_a_near_empty_portfolio_cannot_trade(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        portfolio.cash = 1.0
        # Lower the high-water mark too, or the drawdown circuit breaker fires
        # first and masks the sizing guards this test is about.
        portfolio.peak_equity = 1.0
        portfolio.starting_cash = 1.0
        db.flush()
        report = OrderManager(db).open_position(
            portfolio, securities[0], confidence=0.9, commit=False
        )
        assert not report.executed
        # Either guard is a correct refusal; what matters is that no dust
        # position is opened.
        assert any(
            phrase in (report.reason or "").lower()
            for phrase in ("cash", "minimum order notional")
        )

    def test_dust_orders_are_refused(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        # Enough equity to pass the drawdown guard, too little to size a real
        # position: the minimum-notional floor must catch it.
        portfolio.cash = 50.0
        portfolio.peak_equity = 55.0
        portfolio.starting_cash = 55.0
        db.flush()
        report = OrderManager(db).open_position(
            portfolio, securities[0], confidence=0.9, commit=False
        )
        assert not report.executed
        assert "minimum order notional" in (report.reason or "")

    def test_a_drawdown_breach_halts_entries(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        # Equity far below the high-water mark trips the circuit breaker.
        portfolio.peak_equity = 5_000_000
        db.flush()
        report = OrderManager(db).open_position(
            portfolio, securities[0], confidence=0.9, commit=False
        )
        assert not report.executed
        assert "drawdown" in (report.reason or "").lower()

    def test_holding_the_same_security_twice_is_refused(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        manager = OrderManager(db)
        manager.open_position(portfolio, securities[0], confidence=0.8, commit=False)
        report = manager.open_position(
            portfolio, securities[0], confidence=0.8, nonce="second", commit=False
        )
        assert not report.executed
        assert "already holding" in (report.reason or "")

    def test_position_size_respects_the_equity_cap(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        report = OrderManager(db).open_position(
            portfolio, securities[0], confidence=1.0, commit=False
        )
        notional = report.quantity * report.price
        assert notional <= 1_000_000 * settings.risk_max_position_pct * 1.001


class TestPortfolioAccounting:
    def test_equity_equals_cash_plus_positions(self, trading_setup):
        db, _, portfolio, securities = trading_setup
        OrderManager(db).open_position(portfolio, securities[0], confidence=0.8, commit=False)
        db.flush()
        view = PortfolioEngine(db).value(portfolio)
        assert view.equity == pytest.approx(view.cash + view.positions_value, abs=0.01)

    def test_round_trip_loses_exactly_the_costs(self, trading_setup):
        """With an unchanged price, a round trip must cost commission + slippage."""
        db, _, portfolio, securities = trading_setup
        manager = OrderManager(db)
        starting = float(portfolio.cash)

        opened = manager.open_position(
            portfolio, securities[0], confidence=0.8, commit=False
        )
        db.flush()
        holding = db.scalars(
            select(Holding).where(Holding.status == PositionStatus.OPEN)
        ).first()
        closed = manager.close_position(portfolio, holding, commit=False)
        db.flush()

        quantity = opened.quantity
        reference = float(db.get(Quote, securities[0].id).price)
        slippage = reference * settings.slippage_bps / 10_000 * quantity * 2
        commission = (opened.price + closed.price) * quantity * settings.commission_bps / 10_000
        assert float(portfolio.cash) == pytest.approx(
            starting - slippage - commission, rel=1e-6
        )


class TestApi:
    @pytest.fixture
    def client(self, seeded_db):
        from app.database.session import get_db
        from app.main import app

        app.dependency_overrides[get_db] = lambda: seeded_db
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def test_health_endpoint_is_public(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] in ("HEALTHY", "DEGRADED", "UNHEALTHY")

    def test_protected_routes_require_authentication(self, client):
        for path in ("/api/v1/portfolio", "/api/v1/models", "/api/v1/auth/me"):
            assert client.get(path).status_code == 401

    def test_register_login_and_access(self, client):
        registration = client.post(
            "/api/v1/auth/register",
            json={"email": "apitest@example.com", "password": "a-strong-test-passphrase"},
        )
        assert registration.status_code == 201

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "apitest@example.com", "password": "a-strong-test-passphrase"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["email"] == "apitest@example.com"

    def test_a_wrong_password_is_rejected(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "wrongpw@example.com", "password": "a-strong-test-passphrase"},
        )
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "wrongpw@example.com", "password": "definitely-not-it"},
        )
        assert response.status_code == 401

    def test_registration_does_not_reveal_existing_accounts(self, client):
        payload = {"email": "dupe@example.com", "password": "a-strong-test-passphrase"}
        assert client.post("/api/v1/auth/register", json=payload).status_code == 201
        second = client.post("/api/v1/auth/register", json=payload)
        assert second.status_code == 400
        assert "exist" not in second.json()["detail"].lower()

    def test_a_weak_password_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
        )
        assert response.status_code == 422

    def test_security_headers_are_present(self, client):
        headers = client.get("/api/v1/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "X-Request-ID" in headers

    def test_market_status_reports_every_exchange(self, client):
        response = client.get("/api/v1/market/status")
        assert response.status_code == 200
        codes = {row["exchange"] for row in response.json()}
        assert {"NSE", "BSE", "NYSE", "NASDAQ"} <= codes

    def test_an_unknown_symbol_is_a_404(self, client):
        assert client.get("/api/v1/market/quote/NOSUCHTICKER").status_code == 404
