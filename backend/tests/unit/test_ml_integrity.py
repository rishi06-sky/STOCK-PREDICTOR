"""The tests that decide whether any model result can be believed.

Two properties matter more than any metric:

* On unpredictable data, the pipeline must score ~0.5 AUC. Anything higher
  means it is reading the answer from somewhere it should not.
* On data with a genuine effect, it must score meaningfully above 0.5.
  A pipeline that always returns 0.5 is broken, not safe.

Both directions are asserted; either alone proves nothing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ml.dataset import Dataset, make_labels
from app.ml.metrics import evaluate_classification, expected_calibration_error
from app.ml.models import get_spec
from app.ml.trainer import train_model
from app.ml.validation import WalkForwardSplitter, verify_fold_integrity
from app.technical.features import FEATURE_SET_VERSION, FeatureConfig, compute_features


def _build_dataset(panel: dict[int, pd.DataFrame], horizon: int = 5) -> Dataset:
    frames_x, frames_y, frames_meta = [], [], []
    for security_id, prices in panel.items():
        features = compute_features(prices, FeatureConfig())
        labels = make_labels(prices["close"], horizon, kind="classification")
        combined = features.join(labels).dropna()
        frames_x.append(combined.drop(columns=["label"]))
        frames_y.append(combined["label"])
        frames_meta.append(
            pd.DataFrame(
                {
                    "security_id": security_id,
                    "symbol": f"S{security_id}",
                    "trade_date": combined.index.date,
                    "close": prices.loc[combined.index, "close"].values,
                },
                index=combined.index,
            )
        )
    X = pd.concat(frames_x)
    y = pd.concat(frames_y)
    meta = pd.concat(frames_meta)
    order = np.lexsort((meta["security_id"].values, meta["trade_date"].values))
    return Dataset(
        X=X.iloc[order].reset_index(drop=True),
        y=y.iloc[order].reset_index(drop=True),
        meta=meta.iloc[order].reset_index(drop=True),
        horizon_days=horizon,
        feature_set_version=FEATURE_SET_VERSION,
        target_kind="classification",
    )


def _momentum_panel(n_securities: int = 6, n: int = 900) -> dict[int, pd.DataFrame]:
    """Series carrying a real, learnable momentum effect."""
    rng = np.random.default_rng(11)
    panel = {}
    for sid in range(n_securities):
        index = pd.bdate_range("2021-01-01", periods=n)
        levels = np.empty(n)
        levels[0] = 100.0
        shocks = rng.normal(0.0002, 0.011, n)
        for t in range(1, n):
            lookback = max(0, t - 11)
            momentum = (levels[t - 1] / levels[lookback] - 1) if t > 11 else 0.0
            levels[t] = levels[t - 1] * (1 + 0.45 * momentum / 10 + shocks[t])
        close = pd.Series(levels, index=index)
        panel[sid] = pd.DataFrame(
            {
                "open": close.shift(1).bfill(),
                "high": close * 1.006,
                "low": close * 0.994,
                "close": close,
                "volume": rng.integers(1e5, 1e6, n).astype(float),
            },
            index=index,
        )
    return panel


class TestLabelAlignment:
    def test_label_is_a_forward_return(self):
        close = pd.Series([100.0, 101, 102, 103, 104, 105])
        labels = make_labels(close, 2, kind="regression")
        assert labels.iloc[0] == pytest.approx(102 / 100 - 1)
        assert labels.iloc[1] == pytest.approx(103 / 101 - 1)

    def test_trailing_rows_have_no_label(self):
        close = pd.Series([100.0, 101, 102, 103, 104, 105])
        labels = make_labels(close, 2, kind="regression")
        assert labels.iloc[-2:].isna().all()

    def test_classification_label_matches_sign(self):
        close = pd.Series([100.0, 90.0, 110.0, 105.0])
        labels = make_labels(close, 1, kind="classification")
        assert labels.iloc[0] == 0.0   # 100 -> 90
        assert labels.iloc[1] == 1.0   # 90 -> 110
        assert labels.iloc[2] == 0.0   # 110 -> 105

    def test_horizon_must_be_positive(self):
        with pytest.raises(ValueError):
            make_labels(pd.Series([1.0, 2.0]), 0)


class TestWalkForwardSplits:
    def test_train_always_precedes_test(self):
        dates = pd.Series(np.repeat(pd.bdate_range("2022-01-03", periods=400).values, 4))
        splitter = WalkForwardSplitter(n_splits=5, embargo_days=5, min_train_dates=150)
        folds = list(splitter.split(dates))
        assert len(folds) == 5
        for fold in folds:
            verify_fold_integrity(fold, dates)

    def test_embargo_gap_is_enforced(self):
        dates = pd.Series(np.repeat(pd.bdate_range("2022-01-03", periods=400).values, 4))
        splitter = WalkForwardSplitter(n_splits=4, embargo_days=10, min_train_dates=150)
        for fold in splitter.split(dates):
            train_end = dates.iloc[fold.train_idx].max()
            test_start = dates.iloc[fold.test_idx].min()
            # Ten trading days is at least twelve calendar days.
            assert (test_start - train_end).days >= 12

    def test_no_date_is_in_both_sides(self):
        dates = pd.Series(np.repeat(pd.bdate_range("2022-01-03", periods=400).values, 4))
        splitter = WalkForwardSplitter(n_splits=5, embargo_days=5, min_train_dates=150)
        for fold in splitter.split(dates):
            train_dates = set(dates.iloc[fold.train_idx])
            test_dates = set(dates.iloc[fold.test_idx])
            assert not (train_dates & test_dates)

    def test_holdout_is_strictly_later(self):
        dates = pd.Series(np.repeat(pd.bdate_range("2022-01-03", periods=400).values, 4))
        splitter = WalkForwardSplitter(n_splits=5, embargo_days=5, min_train_dates=150)
        train_idx, holdout_idx = splitter.holdout_split(dates, 0.2)
        assert dates.iloc[train_idx].max() < dates.iloc[holdout_idx].min()

    def test_too_little_history_is_refused(self):
        dates = pd.Series(pd.bdate_range("2024-01-01", periods=30).values)
        with pytest.raises(ValueError, match="distinct dates"):
            list(WalkForwardSplitter(n_splits=5, min_train_dates=150).split(dates))


class TestLeakageCanary:
    """The decisive test: unpredictable data must stay unpredictable."""

    @pytest.mark.slow
    @pytest.mark.parametrize("model", ["logistic_regression", "random_forest", "lightgbm"])
    def test_random_walk_is_not_predictable(self, random_walk_panel, model):
        dataset = _build_dataset(random_walk_panel)
        result = train_model(dataset, get_spec(model), n_splits=3, embargo_days=5)
        auc = result.cv_mean.get("roc_auc")
        assert auc is not None
        assert auc < 0.56, (
            f"{model} scored {auc:.4f} AUC on a pure random walk. The future is "
            "independent of the past here, so anything meaningfully above 0.5 "
            "means the pipeline is leaking information."
        )

    @pytest.mark.slow
    def test_random_walk_model_is_not_promotable(self, random_walk_panel):
        from app.ml.registry import PromotionGate

        dataset = _build_dataset(random_walk_panel)
        result = train_model(dataset, get_spec("logistic_regression"), n_splits=3)
        passed, failures = PromotionGate().evaluate(result)
        assert not passed, "a model with no edge must never clear the promotion gate"
        assert failures


class TestSignalDetection:
    """The other half: a working pipeline must find a real effect."""

    @pytest.mark.slow
    def test_momentum_effect_is_detected(self):
        dataset = _build_dataset(_momentum_panel())
        result = train_model(dataset, get_spec("logistic_regression"), n_splits=3, embargo_days=5)
        auc = result.cv_mean.get("roc_auc")
        assert auc > 0.55, (
            f"only {auc:.4f} AUC on data with a deliberately injected momentum "
            "effect -- the pipeline is failing to learn what is there"
        )
        assert result.beats_baseline

    @pytest.mark.slow
    def test_detected_model_is_promotable(self):
        from app.ml.registry import PromotionGate

        dataset = _build_dataset(_momentum_panel())
        result = train_model(dataset, get_spec("logistic_regression"), n_splits=3)
        passed, failures = PromotionGate().evaluate(result)
        assert passed, f"a model with a genuine edge was rejected: {failures}"


class TestMetricsHonesty:
    def test_majority_class_model_shows_no_lift(self):
        y = np.concatenate([np.ones(540), np.zeros(460)])
        always_up = np.full(1000, 0.9)
        metrics = evaluate_classification(y, always_up)
        assert metrics.accuracy == pytest.approx(0.54)
        assert metrics.baseline_accuracy == pytest.approx(0.54)
        assert metrics.lift_over_baseline == pytest.approx(0.0)
        assert not metrics.beats_baseline

    def test_calibration_error_penalises_overconfidence(self):
        rng = np.random.default_rng(0)
        y = (rng.random(2000) < 0.5).astype(float)
        overconfident = np.where(y > 0, 0.99, 0.01) * 0 + 0.95   # always claims 95%
        assert expected_calibration_error(y, overconfident) > 0.4

    def test_calibration_error_is_low_when_honest(self):
        rng = np.random.default_rng(1)
        probabilities = rng.uniform(0.05, 0.95, 20000)
        outcomes = (rng.random(20000) < probabilities).astype(float)
        assert expected_calibration_error(outcomes, probabilities) < 0.05

    def test_roc_auc_is_absent_for_a_single_class(self):
        y = np.ones(50)
        metrics = evaluate_classification(y, np.full(50, 0.8))
        assert metrics.roc_auc is None
