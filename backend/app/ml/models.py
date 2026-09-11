"""Model factory.

Baselines first. Gradient boosting usually wins on tabular financial features,
but a logistic regression that beats it is genuinely informative -- it says the
signal is mostly linear and the boosted model is fitting noise. Deep learning
is deliberately absent: with a few thousand rows and ~30 noisy features it has
no advantage here, and claiming otherwise would be marketing, not engineering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import (
    GradientBoostingClassifier, RandomForestClassifier, RandomForestRegressor,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.core.logging import get_logger

log = get_logger(__name__)

try:
    from lightgbm import LGBMClassifier, LGBMRegressor
    HAS_LIGHTGBM = True
except ImportError:  # pragma: no cover
    HAS_LIGHTGBM = False

try:
    from xgboost import XGBClassifier, XGBRegressor
    HAS_XGBOOST = True
except ImportError:  # pragma: no cover
    HAS_XGBOOST = False


@dataclass(slots=True)
class ModelSpec:
    name: str
    estimator: Any
    kind: str                       # "classification" | "regression"
    needs_scaling: bool = False
    param_grid: dict = field(default_factory=dict)
    is_baseline: bool = False


def _wrap(estimator, *, needs_scaling: bool) -> Pipeline:
    """Impute, optionally scale, then fit.

    Imputation is inside the pipeline on purpose: fitting it on the whole
    dataset before splitting would leak test-set statistics into training.
    """
    steps = [("impute", SimpleImputer(strategy="median"))]
    if needs_scaling:
        steps.append(("scale", StandardScaler()))
    steps.append(("model", estimator))
    return Pipeline(steps)


def classification_specs(*, seed: int = 42) -> list[ModelSpec]:
    specs = [
        ModelSpec(
            name="majority_baseline",
            estimator=DummyClassifier(strategy="prior"),
            kind="classification", is_baseline=True,
        ),
        ModelSpec(
            name="logistic_regression",
            estimator=LogisticRegression(max_iter=2000, C=0.5, random_state=seed),
            kind="classification", needs_scaling=True,
            param_grid={"model__C": [0.05, 0.1, 0.5, 1.0]},
        ),
        ModelSpec(
            name="random_forest",
            estimator=RandomForestClassifier(
                n_estimators=300, max_depth=6, min_samples_leaf=25,
                n_jobs=-1, random_state=seed, class_weight="balanced_subsample",
            ),
            kind="classification",
            param_grid={"model__max_depth": [4, 6, 8], "model__min_samples_leaf": [10, 25, 50]},
        ),
        ModelSpec(
            name="gradient_boosting",
            estimator=GradientBoostingClassifier(
                n_estimators=200, learning_rate=0.05, max_depth=3,
                subsample=0.8, random_state=seed,
            ),
            kind="classification",
            param_grid={"model__learning_rate": [0.02, 0.05, 0.1], "model__max_depth": [2, 3, 4]},
        ),
    ]

    if HAS_XGBOOST:
        specs.append(
            ModelSpec(
                name="xgboost",
                estimator=XGBClassifier(
                    n_estimators=300, learning_rate=0.05, max_depth=4,
                    subsample=0.8, colsample_bytree=0.8,
                    reg_lambda=1.5, min_child_weight=10,
                    eval_metric="logloss", random_state=seed, n_jobs=-1,
                    tree_method="hist",
                ),
                kind="classification",
                param_grid={"model__max_depth": [3, 4, 6], "model__learning_rate": [0.02, 0.05, 0.1]},
            )
        )
    if HAS_LIGHTGBM:
        specs.append(
            ModelSpec(
                name="lightgbm",
                estimator=LGBMClassifier(
                    n_estimators=300, learning_rate=0.05, max_depth=5,
                    num_leaves=24, min_child_samples=30, subsample=0.8,
                    colsample_bytree=0.8, reg_lambda=1.5,
                    random_state=seed, n_jobs=-1, verbose=-1,
                ),
                kind="classification",
                param_grid={"model__num_leaves": [15, 24, 40], "model__learning_rate": [0.02, 0.05, 0.1]},
            )
        )
    return specs


def regression_specs(*, seed: int = 42) -> list[ModelSpec]:
    specs = [
        ModelSpec(
            name="mean_baseline", estimator=DummyRegressor(strategy="mean"),
            kind="regression", is_baseline=True,
        ),
        ModelSpec(
            name="ridge", estimator=Ridge(alpha=1.0, random_state=seed),
            kind="regression", needs_scaling=True,
            param_grid={"model__alpha": [0.1, 1.0, 10.0]},
        ),
        ModelSpec(
            name="random_forest_reg",
            estimator=RandomForestRegressor(
                n_estimators=250, max_depth=6, min_samples_leaf=25,
                n_jobs=-1, random_state=seed,
            ),
            kind="regression",
        ),
    ]
    if HAS_XGBOOST:
        specs.append(
            ModelSpec(
                name="xgboost_reg",
                estimator=XGBRegressor(
                    n_estimators=250, learning_rate=0.05, max_depth=4,
                    subsample=0.8, colsample_bytree=0.8, reg_lambda=1.5,
                    random_state=seed, n_jobs=-1, tree_method="hist",
                ),
                kind="regression",
            )
        )
    return specs


def build_pipeline(spec: ModelSpec) -> Pipeline:
    return _wrap(spec.estimator, needs_scaling=spec.needs_scaling)


def get_spec(name: str, kind: str = "classification", *, seed: int = 42) -> ModelSpec:
    pool = classification_specs(seed=seed) if kind == "classification" else regression_specs(seed=seed)
    for spec in pool:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown {kind} model {name!r}; available: {[s.name for s in pool]}")


def available_models(kind: str = "classification") -> list[str]:
    pool = classification_specs() if kind == "classification" else regression_specs()
    return [s.name for s in pool]


def feature_importances(pipeline: Pipeline, feature_names: list[str]) -> dict[str, float]:
    """Normalised importances, whatever the underlying estimator supports."""
    model = pipeline.named_steps.get("model")
    if model is None:
        return {}

    values: np.ndarray | None = None
    if hasattr(model, "feature_importances_"):
        values = np.asarray(model.feature_importances_, dtype=float)
    elif hasattr(model, "coef_"):
        values = np.abs(np.asarray(model.coef_, dtype=float)).ravel()

    if values is None or len(values) != len(feature_names):
        return {}
    total = values.sum()
    if total <= 0:
        return {}
    return {
        name: round(float(value / total), 6)
        for name, value in sorted(
            zip(feature_names, values), key=lambda kv: kv[1], reverse=True
        )
    }
