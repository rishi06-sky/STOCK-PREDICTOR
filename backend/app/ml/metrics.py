"""Evaluation metrics.

Classification accuracy alone is close to useless for trading models: a market
that rises 54% of days makes a "always up" predictor look 54% accurate. These
metrics therefore always report the majority-class baseline alongside, plus
calibration quality and the return actually achievable by acting on the
signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.metrics import (
    accuracy_score, brier_score_loss, f1_score, log_loss, mean_absolute_error,
    precision_score, r2_score, recall_score, roc_auc_score,
)


@dataclass(slots=True)
class ClassificationMetrics:
    accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: float | None
    brier: float | None
    log_loss: float | None
    baseline_accuracy: float
    positive_rate: float
    predicted_positive_rate: float
    sample_size: int
    calibration_error: float | None = None
    lift_over_baseline: float = 0.0
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            k: (round(v, 6) if isinstance(v, float) else v)
            for k, v in {
                "accuracy": self.accuracy, "precision": self.precision,
                "recall": self.recall, "f1": self.f1, "roc_auc": self.roc_auc,
                "brier": self.brier, "log_loss": self.log_loss,
                "baseline_accuracy": self.baseline_accuracy,
                "positive_rate": self.positive_rate,
                "predicted_positive_rate": self.predicted_positive_rate,
                "calibration_error": self.calibration_error,
                "lift_over_baseline": self.lift_over_baseline,
                "sample_size": self.sample_size,
                **self.extras,
            }.items()
        }

    @property
    def beats_baseline(self) -> bool:
        return self.lift_over_baseline > 0


@dataclass(slots=True)
class RegressionMetrics:
    mae: float
    rmse: float
    r2: float
    directional_accuracy: float
    sample_size: int

    def as_dict(self) -> dict:
        return {
            "mae": round(self.mae, 8), "rmse": round(self.rmse, 8),
            "r2": round(self.r2, 6),
            "directional_accuracy": round(self.directional_accuracy, 6),
            "sample_size": self.sample_size,
        }


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> float:
    """Mean gap between predicted probability and realised frequency.

    A model claiming 80% confidence should be right 80% of the time; ECE
    measures how far from that it is.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    if len(y_true) == 0:
        return float("nan")

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    error, total = 0.0, len(y_true)
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob > lo) & (y_prob <= hi) if lo > 0 else (y_prob >= lo) & (y_prob <= hi)
        count = int(mask.sum())
        if count == 0:
            continue
        error += (count / total) * abs(y_true[mask].mean() - y_prob[mask].mean())
    return float(error)


def reliability_table(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> list[dict]:
    """Per-bin predicted vs. realised frequency, for the monitoring UI."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob > lo) & (y_prob <= hi) if lo > 0 else (y_prob >= lo) & (y_prob <= hi)
        count = int(mask.sum())
        rows.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "count": count,
                "predicted": round(float(y_prob[mask].mean()), 4) if count else None,
                "actual": round(float(y_true[mask].mean()), 4) if count else None,
            }
        )
    return rows


def evaluate_classification(
    y_true, y_prob, *, threshold: float = 0.5
) -> ClassificationMetrics:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    y_pred = (y_prob >= threshold).astype(float)

    positive_rate = float(y_true.mean()) if len(y_true) else 0.0
    # The score to beat: always predict whichever class is more common.
    baseline = max(positive_rate, 1 - positive_rate)
    accuracy = float(accuracy_score(y_true, y_pred))

    single_class = len(np.unique(y_true)) < 2
    return ClassificationMetrics(
        accuracy=accuracy,
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        roc_auc=None if single_class else float(roc_auc_score(y_true, y_prob)),
        brier=float(brier_score_loss(y_true, y_prob)),
        log_loss=(
            None if single_class
            else float(log_loss(y_true, np.clip(y_prob, 1e-7, 1 - 1e-7), labels=[0, 1]))
        ),
        baseline_accuracy=baseline,
        positive_rate=positive_rate,
        predicted_positive_rate=float(y_pred.mean()) if len(y_pred) else 0.0,
        calibration_error=expected_calibration_error(y_true, y_prob),
        lift_over_baseline=accuracy - baseline,
        sample_size=int(len(y_true)),
    )


def evaluate_regression(y_true, y_pred) -> RegressionMetrics:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return RegressionMetrics(
        mae=float(mean_absolute_error(y_true, y_pred)),
        rmse=float(np.sqrt(np.mean((y_true - y_pred) ** 2))),
        r2=float(r2_score(y_true, y_pred)) if len(y_true) > 1 else float("nan"),
        directional_accuracy=(
            float(np.mean(np.sign(y_true) == np.sign(y_pred))) if len(y_true) else 0.0
        ),
        sample_size=int(len(y_true)),
    )


def trading_metrics(
    y_prob: np.ndarray, forward_returns: np.ndarray, *,
    threshold: float = 0.55, cost_bps: float = 8.0,
) -> dict:
    """What acting on the signal would have returned, after costs.

    This is a signal-quality diagnostic on single-period returns, not a
    portfolio backtest -- the backtesting engine handles sizing and timing.
    """
    y_prob = np.asarray(y_prob, dtype=float)
    forward_returns = np.asarray(forward_returns, dtype=float)
    taken = y_prob >= threshold
    n_trades = int(taken.sum())

    if n_trades == 0:
        return {
            "trades": 0, "hit_rate": None, "mean_return": None,
            "mean_return_net": None, "total_return_net": None,
            "market_mean_return": float(forward_returns.mean()) if len(forward_returns) else None,
            "note": f"no prediction reached the {threshold} threshold",
        }

    selected = forward_returns[taken]
    cost = cost_bps / 10_000.0
    net = selected - cost
    wins = net[net > 0]
    losses = net[net <= 0]

    return {
        "trades": n_trades,
        "hit_rate": round(float((selected > 0).mean()), 4),
        "mean_return": round(float(selected.mean()), 6),
        "mean_return_net": round(float(net.mean()), 6),
        "total_return_net": round(float(net.sum()), 6),
        "market_mean_return": round(float(forward_returns.mean()), 6),
        "edge_vs_market": round(float(net.mean() - forward_returns.mean()), 6),
        "profit_factor": (
            round(float(wins.sum() / abs(losses.sum())), 4)
            if losses.size and losses.sum() != 0 else None
        ),
        "selectivity": round(n_trades / len(forward_returns), 4),
    }
