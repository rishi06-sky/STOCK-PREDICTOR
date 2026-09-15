"""Training and validation orchestration.

The sequence is deliberate:

  walk-forward CV  ->  select on out-of-fold score  ->  calibrate  ->  holdout

Model selection uses out-of-fold results only. The holdout block is scored
exactly once, at the end, and never informs a choice -- otherwise it stops
being out-of-sample and every number it produces is optimistic.

A candidate that cannot beat the majority-class baseline is reported as such
and is not promoted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV

from app.core.config import settings
from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.ml.dataset import Dataset
from app.ml.metrics import (
    ClassificationMetrics, evaluate_classification, evaluate_regression,
    reliability_table, trading_metrics,
)
from app.ml.models import (
    ModelSpec, build_pipeline, classification_specs, feature_importances,
    regression_specs,
)
from app.ml.validation import WalkForwardSplitter, verify_fold_integrity

log = get_logger(__name__)


@dataclass(slots=True)
class FoldResult:
    fold: int
    metrics: dict
    train_rows: int
    test_rows: int
    train_period: str
    test_period: str


@dataclass(slots=True)
class TrainingResult:
    model_name: str
    algorithm: str
    kind: str
    horizon_days: int
    feature_set_version: str
    feature_names: list[str]
    cv_folds: list[FoldResult] = field(default_factory=list)
    cv_mean: dict = field(default_factory=dict)
    cv_std: dict = field(default_factory=dict)
    holdout: dict = field(default_factory=dict)
    reliability: list[dict] = field(default_factory=list)
    importances: dict = field(default_factory=dict)
    trading: dict = field(default_factory=dict)
    calibrated: bool = False
    pipeline: object | None = None
    training_rows: int = 0
    train_start: object = None
    train_end: object = None
    feature_baseline: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary_score(self) -> float:
        """Score used for model selection. Out-of-fold only."""
        if self.kind == "classification":
            return float(self.cv_mean.get("roc_auc") or 0.0)
        return float(self.cv_mean.get("r2") or -1e9)

    @property
    def beats_baseline(self) -> bool:
        if self.kind == "classification":
            return float(self.cv_mean.get("lift_over_baseline", 0.0)) > 0.0
        return float(self.cv_mean.get("r2", -1.0)) > 0.0

    def summary(self) -> dict:
        return {
            "model": self.model_name, "algorithm": self.algorithm,
            "horizon_days": self.horizon_days, "kind": self.kind,
            "calibrated": self.calibrated, "training_rows": self.training_rows,
            "cv_mean": self.cv_mean, "holdout": self.holdout,
            "beats_baseline": self.beats_baseline,
            "warnings": self.warnings,
        }


def _aggregate(fold_metrics: list[dict]) -> tuple[dict, dict]:
    if not fold_metrics:
        return {}, {}
    keys = [
        k for k in fold_metrics[0]
        if isinstance(fold_metrics[0][k], (int, float)) and fold_metrics[0][k] is not None
    ]
    mean, std = {}, {}
    for key in keys:
        values = [m[key] for m in fold_metrics if isinstance(m.get(key), (int, float))]
        if values:
            mean[key] = round(float(np.mean(values)), 6)
            std[key] = round(float(np.std(values)), 6)
    return mean, std


def _feature_baseline(X: pd.DataFrame) -> dict:
    """Per-feature distribution snapshot, later compared against live data."""
    described = X.describe().to_dict()
    return {
        name: {
            "mean": round(float(stats.get("mean", 0.0)), 8),
            "std": round(float(stats.get("std", 0.0) or 0.0), 8),
            "p25": round(float(stats.get("25%", 0.0)), 8),
            "p50": round(float(stats.get("50%", 0.0)), 8),
            "p75": round(float(stats.get("75%", 0.0)), 8),
        }
        for name, stats in described.items()
    }


def train_model(
    dataset: Dataset, spec: ModelSpec, *, n_splits: int | None = None,
    embargo_days: int | None = None, calibrate: bool = True,
    holdout_fraction: float = 0.2, cost_bps: float = 8.0,
) -> TrainingResult:
    """Walk-forward evaluate one spec, then fit it for production."""
    n_splits = n_splits or settings.ml_walk_forward_splits
    embargo_days = embargo_days if embargo_days is not None else settings.ml_embargo_days

    if dataset.n_rows < settings.ml_min_training_rows:
        raise InsufficientDataError(
            f"{dataset.n_rows} rows available, {settings.ml_min_training_rows} required"
        )

    X, y = dataset.X, dataset.y
    dates = pd.Series(pd.to_datetime(dataset.meta["trade_date"]))
    result = TrainingResult(
        model_name=spec.name, algorithm=type(spec.estimator).__name__, kind=spec.kind,
        horizon_days=dataset.horizon_days,
        feature_set_version=dataset.feature_set_version,
        feature_names=list(X.columns),
    )

    # ------------------------------------------------------- carve out holdout
    splitter = WalkForwardSplitter(
        n_splits=n_splits, embargo_days=embargo_days,
        min_train_dates=max(60, int(dates.nunique() * 0.35)),
    )
    dev_idx, holdout_idx = splitter.holdout_split(dates, holdout_fraction)
    if len(dev_idx) == 0 or len(holdout_idx) == 0:
        raise InsufficientDataError("not enough distinct dates to reserve a holdout block")

    X_dev, y_dev, dates_dev = X.iloc[dev_idx], y.iloc[dev_idx], dates.iloc[dev_idx]

    # ------------------------------------------------------- walk-forward CV
    fold_metrics: list[dict] = []
    oof_true, oof_prob = [], []

    dev_splitter = WalkForwardSplitter(
        n_splits=n_splits, embargo_days=embargo_days,
        min_train_dates=max(40, int(dates_dev.nunique() * 0.4)),
    )
    try:
        folds = list(dev_splitter.split(dates_dev))
    except ValueError as exc:
        result.warnings.append(f"reduced cross-validation: {exc}")
        dev_splitter = WalkForwardSplitter(
            n_splits=2, embargo_days=embargo_days,
            min_train_dates=max(20, int(dates_dev.nunique() * 0.5)),
        )
        folds = list(dev_splitter.split(dates_dev))

    for fold in folds:
        verify_fold_integrity(fold, dates_dev)
        pipeline = build_pipeline(spec)
        X_tr, y_tr = X_dev.iloc[fold.train_idx], y_dev.iloc[fold.train_idx]
        X_te, y_te = X_dev.iloc[fold.test_idx], y_dev.iloc[fold.test_idx]

        if spec.kind == "classification" and len(np.unique(y_tr)) < 2:
            result.warnings.append(f"fold {fold.index}: single-class training window, skipped")
            continue

        pipeline.fit(X_tr, y_tr)
        if spec.kind == "classification":
            prob = pipeline.predict_proba(X_te)[:, 1]
            metrics = evaluate_classification(y_te, prob).as_dict()
            oof_true.append(np.asarray(y_te, dtype=float))
            oof_prob.append(prob)
        else:
            pred = pipeline.predict(X_te)
            metrics = evaluate_regression(y_te, pred).as_dict()

        fold_metrics.append(metrics)
        result.cv_folds.append(
            FoldResult(
                fold=fold.index, metrics=metrics,
                train_rows=len(fold.train_idx), test_rows=len(fold.test_idx),
                train_period=f"{fold.train_start}..{fold.train_end}",
                test_period=f"{fold.test_start}..{fold.test_end}",
            )
        )

    if not fold_metrics:
        raise InsufficientDataError("no usable cross-validation fold was produced")

    result.cv_mean, result.cv_std = _aggregate(fold_metrics)

    # ---------------------------------------------------- fit + calibrate
    final = build_pipeline(spec)
    if spec.kind == "classification" and calibrate and not spec.is_baseline:
        # Isotonic needs volume; Platt scaling is the stable choice below ~2k rows.
        method = "isotonic" if len(X_dev) >= 2000 else "sigmoid"
        try:
            # cv=3 here is a time-agnostic internal split of the DEV block only.
            # The holdout block is untouched, so this cannot leak into reported
            # out-of-sample numbers.
            final = CalibratedClassifierCV(final, method=method, cv=3)
            final.fit(X_dev, y_dev)
            result.calibrated = True
        except Exception as exc:
            result.warnings.append(f"calibration failed ({exc}); using uncalibrated model")
            final = build_pipeline(spec)
            final.fit(X_dev, y_dev)
    else:
        final.fit(X_dev, y_dev)

    result.pipeline = final
    result.training_rows = int(len(X_dev))
    result.train_start = dates_dev.min().date()
    result.train_end = dates_dev.max().date()
    result.feature_baseline = _feature_baseline(X_dev)

    # ----------------------------------------------- holdout: scored once
    X_ho, y_ho = X.iloc[holdout_idx], y.iloc[holdout_idx]
    if spec.kind == "classification":
        prob_ho = final.predict_proba(X_ho)[:, 1]
        holdout_metrics = evaluate_classification(y_ho, prob_ho)
        result.holdout = holdout_metrics.as_dict()
        result.reliability = reliability_table(np.asarray(y_ho, dtype=float), prob_ho)

        # Signal-quality check on realised forward returns for those same rows.
        forward = _forward_returns(dataset, holdout_idx)
        if forward is not None:
            result.trading = trading_metrics(
                prob_ho, forward, threshold=settings.signal_min_confidence, cost_bps=cost_bps
            )
    else:
        pred_ho = final.predict(X_ho)
        result.holdout = evaluate_regression(y_ho, pred_ho).as_dict()

    # Importances come from the underlying pipeline, calibrated or not.
    base = getattr(final, "estimator", final)
    if hasattr(final, "calibrated_classifiers_") and final.calibrated_classifiers_:
        inner = final.calibrated_classifiers_[0]
        base = getattr(inner, "estimator", base)
    try:
        result.importances = feature_importances(base, list(X.columns))
    except Exception:
        result.importances = {}

    if not result.beats_baseline:
        result.warnings.append(
            "does not beat the majority-class baseline out-of-fold; not fit for promotion"
        )

    return result


def _forward_returns(dataset: Dataset, idx: np.ndarray) -> np.ndarray | None:
    """Realised forward return for the given rows, for trading diagnostics."""
    if dataset.target_kind == "regression":
        return np.asarray(dataset.y.iloc[idx], dtype=float)
    return None


def train_and_select(
    dataset: Dataset, *, specs: list[ModelSpec] | None = None, calibrate: bool = True,
    **kwargs,
) -> tuple[TrainingResult, list[TrainingResult]]:
    """Train every candidate, return (winner, all results) ranked out-of-fold."""
    if specs is None:
        specs = (
            classification_specs()
            if dataset.target_kind == "classification"
            else regression_specs()
        )

    results: list[TrainingResult] = []
    for spec in specs:
        try:
            results.append(train_model(dataset, spec, calibrate=calibrate, **kwargs))
            log.info(
                "model_trained", model=spec.name, score=round(results[-1].primary_score, 4)
            )
        except Exception as exc:
            log.error("model_training_failed", model=spec.name, error=str(exc))

    if not results:
        raise InsufficientDataError("every candidate model failed to train")

    results.sort(key=lambda r: r.primary_score, reverse=True)
    # Never select a baseline as the production model, even if it ranks first --
    # that outcome means there is no signal, and it is reported as such.
    non_baseline = [
        r for r in results
        if r.model_name not in ("majority_baseline", "mean_baseline")
    ]
    winner = non_baseline[0] if non_baseline else results[0]
    return winner, results


# ---------------------------------------------------------------- model store
def model_store_dir() -> Path:
    path = Path(settings.ml_model_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_model(result: TrainingResult, name: str, version: str) -> tuple[str, str]:
    """Persist the fitted pipeline. Returns (path, sha256)."""
    if result.pipeline is None:
        raise ValueError("training result carries no fitted pipeline")

    directory = model_store_dir() / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version}.joblib"
    if path.exists():
        # Versions are allocated from the registry table, so an artefact that
        # is already on disk means the filesystem and the registry disagree --
        # a restored database, a deleted row, or a run pointed at the wrong
        # store. Overwriting would destroy the artefact a live ModelVersion
        # row still references, and the checksum recorded against that row
        # would stop matching, disabling predictions until someone works out
        # why. Refuse instead: a loud failure is recoverable, silent
        # corruption is not.
        raise FileExistsError(
            f"refusing to overwrite existing model artefact {path}: "
            f"version {version} of {name} is already on disk. The registry "
            "and the model store are out of sync -- check that ML_MODEL_DIR "
            "matches the database, and delete the file only once you have "
            "confirmed no model_versions row points at it."
        )
    joblib.dump(
        {
            "pipeline": result.pipeline,
            "feature_names": result.feature_names,
            "feature_set_version": result.feature_set_version,
            "horizon_days": result.horizon_days,
            "kind": result.kind,
            "calibrated": result.calibrated,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        },
        path,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / f"{version}.metrics.json").write_text(
        json.dumps(
            {
                "cv_mean": result.cv_mean, "cv_std": result.cv_std,
                "holdout": result.holdout, "trading": result.trading,
                "warnings": result.warnings, "sha256": digest,
            },
            indent=2,
        )
    )
    return str(path), digest


def load_model(path: str, *, expected_sha256: str | None = None) -> dict:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"model artefact not found: {path}")
    if expected_sha256:
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual != expected_sha256:
            # A changed artefact must never be loaded silently.
            raise ValueError(
                f"model artefact checksum mismatch for {path}: "
                f"expected {expected_sha256[:12]}, got {actual[:12]}"
            )
    return joblib.load(file)
