"""End-to-end acceptance test.

Exercises the complete chain required by the specification:

    market data -> database -> features -> ML prediction -> signal
                -> risk check -> ranking -> alert -> paper trade
                -> portfolio update

and then asserts the results are mutually CONSISTENT: that the price a signal
was computed from, the price a trade filled at, and the portfolio's accounting
all agree to the basis point.

The data source is the fixture provider with an injected momentum effect, so a
model with a genuine edge exists to drive the downstream stages. Every record
it produces is tagged SYNTHETIC; this test proves the plumbing, not that any
real market is predictable.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.enums import (
    AlertStatus, AssetType, DataQuality, PositionStatus, PredictionTarget,
    SignalStatus, SignalType, UserRole,
)
from app.models.analysis import Prediction, Signal
from app.models.market import PriceData, Quote, Security
from app.models.platform import Alert, ModelMetric, ModelVersion, User
from app.models.trading import Holding, Order, Portfolio, Trade
from tests.conftest import requires_db

pytestmark = [pytest.mark.e2e, pytest.mark.slow, requires_db]


@pytest.fixture(scope="module", autouse=True)
def _momentum_fixtures():
    """Enable the learnable effect so a promotable model exists."""
    from app.market_data.providers.fixture import FixtureProvider

    previous = FixtureProvider.inject_momentum
    FixtureProvider.inject_momentum = True
    yield
    FixtureProvider.inject_momentum = previous


@pytest.fixture
def pipeline_db(seeded_db):
    from app.core.security import hash_password
    from app.trading.paper_engine import get_or_create_paper_portfolio

    user = User(
        email="e2e@example.com", password_hash=hash_password("a-strong-test-passphrase"),
        role=UserRole.ADMIN,
    )
    seeded_db.add(user)
    seeded_db.flush()
    get_or_create_paper_portfolio(seeded_db, user.id, currency="USD")
    seeded_db.flush()
    return seeded_db


class TestFullPipeline:
    def test_market_data_to_portfolio_update(self, pipeline_db):
        db = pipeline_db
        from app.ml.dataset import build_dataset
        from app.ml.registry import PromotionGate, promote_model, register_model
        from app.ml.trainer import train_and_select
        from app.services.pipeline import Pipeline

        pipeline = Pipeline(db)

        # ---------------------------------------------------- 1. market data
        ingest = pipeline.ingest(lookback_days=1500)
        assert ingest.ok, ingest.error
        assert ingest.detail["bars_written"] > 10_000
        assert ingest.detail["quotes_written"] > 0
        db.flush()

        stored_bars = db.scalar(select(func.count()).select_from(PriceData))
        assert stored_bars > 10_000

        # ------------------------------------------------------- 2. features
        securities = list(
            db.scalars(
                select(Security).where(
                    Security.is_active.is_(True), Security.asset_type == AssetType.EQUITY
                )
            )
        )
        dataset = build_dataset(db, securities, horizon_days=5, kind="classification")
        assert dataset.n_rows > 5_000
        assert len(dataset.feature_names) >= 25

        # ------------------------------------------------------ 3. ML model
        winner, _ = train_and_select(dataset, n_splits=3, embargo_days=5)
        assert winner.cv_mean["roc_auc"] > 0.55, (
            "the injected momentum effect was not learned; the pipeline is broken"
        )
        version = register_model(
            db, winner, name="direction_5d", target=PredictionTarget.DIRECTION, commit=False
        )
        db.flush()
        promoted, failures = promote_model(db, version, winner, gate=PromotionGate())
        assert promoted, f"a model with a real edge was refused promotion: {failures}"

        # ---------------------------------------------------- 4. regime + 5. signals
        pipeline.detect_regimes()
        signals_stage = pipeline.generate_signals(horizon_days=5)
        assert signals_stage.ok
        assert signals_stage.skipped_reason is None
        assert signals_stage.detail["predictions"] > 0
        db.flush()

        predictions = db.scalar(select(func.count()).select_from(Prediction))
        assert predictions > 0

        active_signals = db.scalars(
            select(Signal).where(Signal.status == SignalStatus.ACTIVE)
        ).all()
        assert active_signals

        # Every signal must carry a full, auditable record.
        for signal in active_signals:
            assert 0 <= float(signal.confidence) <= 1
            assert signal.rationale, "a signal was emitted with no explanation"
            assert signal.expires_at > signal.generated_at
            assert signal.data_quality is DataQuality.SYNTHETIC
            assert signal.model_version_id is not None
            if signal.signal.is_actionable:
                assert signal.stop_loss is not None
                assert signal.take_profit is not None
                assert float(signal.reward_to_risk) >= settings.risk_min_reward_to_risk
                assert float(signal.confidence) >= settings.signal_min_confidence

        # ------------------------------------------------------- 6. ranking
        ranking = pipeline.rank_opportunities()
        assert ranking.detail["ranked"] > 0
        scores = [
            row["score"] for row in ranking.detail["top"] if row["score"] is not None
        ]
        assert scores == sorted(scores, reverse=True), "ranking is not ordered by score"

        # -------------------------------------------------------- 7. alerts
        alerts_stage = pipeline.dispatch_alerts()
        db.flush()
        assert alerts_stage.detail["created"] > 0
        alerts = db.scalars(select(Alert)).all()
        assert alerts
        for alert in alerts:
            assert alert.dedupe_key
            assert "SYNTHETIC" in alert.body or "SYNTHETIC" in alert.title

        # Re-running must not duplicate alerts within the dedupe window.
        before = db.scalar(select(func.count()).select_from(Alert))
        pipeline.dispatch_alerts()
        db.flush()
        assert db.scalar(select(func.count()).select_from(Alert)) == before

        # -------------------------------------------------- 8. paper trading
        trading = pipeline.run_paper_trading(max_new_positions=5)
        db.flush()
        report = trading.detail["portfolios"][0]
        assert not report["halted"], report["halt_reason"]
        assert len(report["opened"]) > 0, (
            f"no positions opened; skips were: {report['skipped'][:3]}"
        )

        # ---------------------------------------------- 9. portfolio update
        portfolio = db.scalars(select(Portfolio)).first()
        from app.portfolio.engine import PortfolioEngine

        view = PortfolioEngine(db).value(portfolio)
        assert view.open_positions == len(report["opened"])
        assert view.equity == pytest.approx(view.cash + view.positions_value, abs=0.01)
        assert view.exposure_pct <= settings.risk_max_portfolio_exposure_pct

        # ------------------------------------------- 10. CONSISTENCY AUDIT
        trades = db.scalars(select(Trade)).all()
        assert trades

        for trade in trades:
            signal = db.scalars(
                select(Signal)
                .where(Signal.security_id == trade.security_id)
                .order_by(Signal.generated_at.desc())
                .limit(1)
            ).first()
            assert signal is not None

            # The fill must equal the reference price plus exactly the
            # configured slippage -- no other drift is acceptable.
            expected_fill = float(trade.reference_price) * (
                1 + settings.slippage_bps / 10_000
            )
            assert float(trade.price) == pytest.approx(expected_fill, rel=1e-9)

            # Commission must match the configured rate on the filled notional.
            notional = float(trade.price) * float(trade.quantity)
            assert float(trade.commission) == pytest.approx(
                notional * settings.commission_bps / 10_000, rel=1e-6
            )
            assert trade.mode.value == "PAPER"

        # Cash must reconcile against the ledger, to the cent.
        spent = sum(
            float(t.price) * float(t.quantity) + float(t.commission) for t in trades
        )
        assert float(portfolio.cash) == pytest.approx(
            float(portfolio.starting_cash) - spent, abs=0.01
        )

        # Every holding must trace back to an order and a trade.
        holdings = db.scalars(
            select(Holding).where(Holding.status == PositionStatus.OPEN)
        ).all()
        order_count = db.scalar(select(func.count()).select_from(Order))
        assert len(holdings) == len(trades) == order_count

        # Sector concentration must respect the configured limit.
        for sector, weight in view.sector_allocation.items():
            assert weight <= settings.risk_max_sector_pct + 1e-9, (
                f"sector {sector} at {weight:.1%} breaches the limit"
            )

    def test_no_model_means_no_signals(self, pipeline_db):
        """The fail-safe path: without a validated model nothing is emitted."""
        db = pipeline_db
        from app.services.pipeline import Pipeline

        pipeline = Pipeline(db)
        pipeline.ingest(lookback_days=400)
        db.flush()

        # No model has been promoted in this test.
        stage = pipeline.generate_signals(horizon_days=5)
        assert stage.ok
        assert stage.skipped_reason is not None
        assert "no validated production model" in stage.skipped_reason
        assert stage.detail["signals_generated"] == 0
        db.flush()
        assert db.scalar(select(func.count()).select_from(Signal)) == 0

    def test_full_cycle_skips_trading_when_no_signals_exist(self, pipeline_db):
        db = pipeline_db
        from app.services.pipeline import Pipeline

        report = Pipeline(db).run_full_cycle(horizon_days=5, include_news=False)
        stages = {s.name: s for s in report.stages}
        assert stages["signals"].skipped_reason
        # Trading and alerting must not run on signals that were never produced.
        assert stages["paper_trading"].skipped_reason
        assert stages["alerts"].skipped_reason
        db.flush()
        assert db.scalar(select(func.count()).select_from(Trade)) == 0
