"""Risk arithmetic, signal classification and confidence discipline."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.enums import DataQuality, OrderSide, RiskLevel, SignalType
from app.risk.engine import (
    classify_risk, position_size, take_profit_from_rr, volatility_stop,
)
from app.signals.engine import (
    MIN_TRUSTWORTHY_AUC, classify_signal, derive_confidence, opportunity_score,
)


class TestPositionSizing:
    def test_risk_per_trade_is_respected(self):
        equity, price, stop = 1_000_000.0, 100.0, 90.0
        quantity, _ = position_size(
            equity, price, stop, risk_per_trade_pct=0.01, max_position_pct=0.50
        )
        # A stop-out must cost 1% of equity.
        assert quantity * (price - stop) == pytest.approx(equity * 0.01)

    def test_wider_stop_means_smaller_position(self):
        equity, price = 1_000_000.0, 100.0
        tight, _ = position_size(equity, price, 95.0, risk_per_trade_pct=0.01, max_position_pct=1.0)
        wide, _ = position_size(equity, price, 80.0, risk_per_trade_pct=0.01, max_position_pct=1.0)
        assert wide < tight
        # Capital at risk is identical either way.
        assert tight * 5 == pytest.approx(wide * 20)

    def test_position_cap_binds_before_risk_target(self):
        equity, price, stop = 1_000_000.0, 100.0, 99.9
        quantity, notes = position_size(
            equity, price, stop, risk_per_trade_pct=0.01, max_position_pct=0.10
        )
        assert quantity * price == pytest.approx(equity * 0.10)
        assert any("capped" in note for note in notes)

    def test_invalid_inputs_produce_no_position(self):
        for equity, price, stop in [(0, 100, 90), (1000, 0, 0), (1000, 100, 100)]:
            quantity, notes = position_size(
                equity, price, stop, risk_per_trade_pct=0.01, max_position_pct=0.1
            )
            assert quantity == 0.0
            assert notes


class TestStopsAndTargets:
    def test_stop_scales_with_volatility(self):
        calm = volatility_stop(100.0, atr=1.0, atr_multiplier=2.0)
        wild = volatility_stop(100.0, atr=5.0, atr_multiplier=2.0)
        assert wild < calm < 100.0

    def test_stop_is_bounded(self):
        # Absurdly wide ATR must not put the stop below the floor.
        assert volatility_stop(100.0, atr=100.0, atr_multiplier=2.0) == pytest.approx(75.0)
        # Absurdly tight ATR must not put the stop on top of the price.
        assert volatility_stop(100.0, atr=0.001, atr_multiplier=2.0) == pytest.approx(98.0)

    def test_short_stop_sits_above_the_price(self):
        assert volatility_stop(100.0, atr=2.0, side=OrderSide.SELL) > 100.0

    def test_take_profit_honours_the_ratio(self):
        target = take_profit_from_rr(100.0, 95.0, reward_to_risk=2.0)
        assert target == pytest.approx(110.0)
        assert (target - 100.0) / (100.0 - 95.0) == pytest.approx(2.0)

    def test_missing_atr_falls_back_to_a_percentage(self):
        assert volatility_stop(100.0, atr=None, fallback_pct=0.08) == pytest.approx(92.0)


class TestRiskClassification:
    @pytest.mark.parametrize(
        "volatility,expected",
        [(0.10, RiskLevel.LOW), (0.25, RiskLevel.MODERATE),
         (0.45, RiskLevel.HIGH), (0.90, RiskLevel.VERY_HIGH)],
    )
    def test_bands(self, volatility, expected):
        assert classify_risk(volatility) is expected

    def test_unknown_volatility_is_treated_as_high(self):
        # Unknown risk is never treated as average risk.
        assert classify_risk(None) is RiskLevel.HIGH


class TestSignalClassification:
    @pytest.mark.parametrize(
        "probability,expected",
        [
            (0.95, SignalType.STRONG_BUY), (0.70, SignalType.STRONG_BUY),
            (0.60, SignalType.BUY), (0.50, SignalType.HOLD),
            (0.45, SignalType.HOLD), (0.40, SignalType.SELL),
            (0.20, SignalType.STRONG_SELL),
        ],
    )
    def test_bands(self, probability, expected):
        assert classify_signal(probability) is expected

    def test_actionability(self):
        assert SignalType.STRONG_BUY.is_actionable
        assert SignalType.SELL.is_actionable
        assert not SignalType.HOLD.is_actionable
        assert not SignalType.NO_ACTION.is_actionable

    def test_direction(self):
        assert SignalType.BUY.direction == 1
        assert SignalType.STRONG_SELL.direction == -1
        assert SignalType.HOLD.direction == 0


class TestConfidence:
    def test_a_good_model_clears_the_floor(self):
        confidence, _ = derive_confidence(
            0.72, model_auc=0.68, data_quality=DataQuality.DELAYED, calibrated=True
        )
        assert confidence >= settings.signal_min_confidence

    def test_a_coin_flip_yields_no_confidence(self):
        confidence, _ = derive_confidence(
            0.50, model_auc=0.65, data_quality=DataQuality.LIVE, calibrated=True
        )
        assert confidence == pytest.approx(0.5, abs=0.01)

    def test_a_worthless_model_is_zeroed(self):
        confidence, notes = derive_confidence(
            0.95, model_auc=0.50, data_quality=DataQuality.LIVE, calibrated=True
        )
        assert confidence == 0.0
        assert any("chance" in note for note in notes)

    def test_an_unvalidated_model_cannot_clear_the_floor(self):
        confidence, notes = derive_confidence(
            0.99, model_auc=None, data_quality=DataQuality.LIVE, calibrated=True
        )
        assert confidence < settings.signal_min_confidence
        assert any("capped" in note for note in notes)

    def test_a_thin_edge_cannot_clear_the_floor(self):
        confidence, _ = derive_confidence(
            0.99, model_auc=MIN_TRUSTWORTHY_AUC, data_quality=DataQuality.LIVE,
            calibrated=True,
        )
        assert confidence < settings.signal_min_confidence

    def test_stale_data_lowers_confidence(self):
        live, _ = derive_confidence(
            0.75, model_auc=0.68, data_quality=DataQuality.LIVE, calibrated=True
        )
        eod, _ = derive_confidence(
            0.75, model_auc=0.68, data_quality=DataQuality.EOD, calibrated=True
        )
        assert eod < live

    def test_uncalibrated_output_lowers_confidence(self):
        calibrated, _ = derive_confidence(
            0.75, model_auc=0.68, data_quality=DataQuality.LIVE, calibrated=True
        )
        raw, _ = derive_confidence(
            0.75, model_auc=0.68, data_quality=DataQuality.LIVE, calibrated=False
        )
        assert raw < calibrated

    def test_confidence_never_exceeds_the_probability(self):
        for probability in (0.55, 0.7, 0.85, 0.99):
            confidence, _ = derive_confidence(
                probability, model_auc=0.95, data_quality=DataQuality.LIVE, calibrated=True
            )
            assert confidence <= probability + 1e-9

    def test_a_down_call_is_as_confident_as_the_mirror_up_call(self):
        up, _ = derive_confidence(
            0.75, model_auc=0.68, data_quality=DataQuality.LIVE, calibrated=True
        )
        down, _ = derive_confidence(
            0.25, model_auc=0.68, data_quality=DataQuality.LIVE, calibrated=True
        )
        assert up == pytest.approx(down)


class TestOpportunityScore:
    def test_risk_reduces_the_score(self):
        low = opportunity_score(0.8, 0.06, RiskLevel.LOW, 2.5)
        high = opportunity_score(0.8, 0.06, RiskLevel.VERY_HIGH, 2.5)
        assert high < low

    def test_confidence_raises_the_score(self):
        assert opportunity_score(0.9, 0.05, RiskLevel.LOW, 2.0) > opportunity_score(
            0.6, 0.05, RiskLevel.LOW, 2.0
        )

    def test_score_is_bounded(self):
        assert 0 <= opportunity_score(1.0, 10.0, RiskLevel.LOW, 100.0) <= 100
        assert 0 <= opportunity_score(0.0, 0.0, RiskLevel.VERY_HIGH, 0.0) <= 100
