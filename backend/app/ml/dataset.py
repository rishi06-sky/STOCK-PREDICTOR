"""Training-set construction.

This module is where look-ahead bias is either prevented or created, so the
alignment rules are stated explicitly:

* Features on row t use only bars up to and including t (guaranteed by the
  causal indicator library).
* The label on row t is computed from bars strictly AFTER t: the return from
  t's close to (t + horizon)'s close.
* The final `horizon` rows therefore have no label and are dropped -- they are
  the live prediction set, not training data.
* Splits are chronological with an embargo gap, never shuffled.

A row is only usable when its features and its label come from opposite sides
of the t boundary. `assert_no_leakage` re-checks that invariant on real data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.models.market import PriceData, Security
from app.technical.features import FEATURE_SET_VERSION, FeatureConfig, compute_features

log = get_logger(__name__)


@dataclass(slots=True)
class Dataset:
    X: pd.DataFrame
    y: pd.Series
    meta: pd.DataFrame              # security_id, trade_date, reference close
    horizon_days: int
    feature_set_version: str
    target_kind: str                # "classification" | "regression"
    feature_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.feature_names:
            self.feature_names = list(self.X.columns)

    @property
    def n_rows(self) -> int:
        return len(self.X)

    @property
    def date_range(self) -> tuple[date, date]:
        return self.meta["trade_date"].min(), self.meta["trade_date"].max()

    def summary(self) -> dict:
        start, end = self.date_range if self.n_rows else (None, None)
        out = {
            "rows": self.n_rows,
            "features": len(self.feature_names),
            "securities": int(self.meta["security_id"].nunique()) if self.n_rows else 0,
            "horizon_days": self.horizon_days,
            "start": str(start), "end": str(end),
            "target_kind": self.target_kind,
        }
        if self.target_kind == "classification" and self.n_rows:
            out["positive_rate"] = round(float(self.y.mean()), 4)
        elif self.n_rows:
            out["label_mean"] = round(float(self.y.mean()), 6)
            out["label_std"] = round(float(self.y.std()), 6)
        return out


def load_price_frame(db: Session, security_id: int, *, end: date | None = None) -> pd.DataFrame:
    """Daily OHLCV for one security as a date-indexed frame."""
    stmt = (
        select(
            PriceData.trade_date, PriceData.open, PriceData.high, PriceData.low,
            PriceData.close, PriceData.adj_close, PriceData.volume,
        )
        .where(PriceData.security_id == security_id)
        .order_by(PriceData.trade_date)
    )
    if end is not None:
        stmt = stmt.where(PriceData.trade_date <= end)

    rows = db.execute(stmt).all()
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "adj_close", "volume"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date").sort_index()
    for column in ("open", "high", "low", "close", "adj_close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    # Prefer adjusted closes: unadjusted series show splits as -50% "returns".
    frame["close"] = frame["adj_close"].where(frame["adj_close"].notna(), frame["close"])
    return frame.drop(columns=["adj_close"])


def make_labels(
    close: pd.Series, horizon_days: int, *, kind: str = "classification",
    threshold: float = 0.0,
) -> pd.Series:
    """Forward return over `horizon_days`, aligned to the decision date.

    `shift(-h)` places the FUTURE close on today's row, which is the whole
    point: the label describes what happens next. It is exactly why the last
    `h` rows must be dropped before training.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    forward_return = close.shift(-horizon_days) / close - 1.0
    if kind == "regression":
        return forward_return.rename("label")
    return (forward_return > threshold).astype("float64").where(
        forward_return.notna()
    ).rename("label")


def build_dataset(
    db: Session,
    securities: list[Security],
    *,
    horizon_days: int,
    kind: str = "classification",
    threshold: float = 0.0,
    end: date | None = None,
    config: FeatureConfig | None = None,
    min_rows_per_security: int | None = None,
) -> Dataset:
    """Assemble a pooled, leakage-free dataset across securities."""
    config = config or FeatureConfig()
    min_rows = min_rows_per_security or (config.min_rows + horizon_days + 10)

    frames_X, frames_y, frames_meta = [], [], []
    skipped: dict[str, str] = {}

    for security in securities:
        prices = load_price_frame(db, security.id, end=end)
        if len(prices) < min_rows:
            skipped[security.symbol] = f"only {len(prices)} bars, need {min_rows}"
            continue

        features = compute_features(prices, config)
        labels = make_labels(prices["close"], horizon_days, kind=kind, threshold=threshold)

        combined = features.join(labels)
        # Dropping NaN removes warm-up rows AND the trailing unlabelled rows.
        combined = combined.dropna()
        if combined.empty:
            skipped[security.symbol] = "no complete rows after warm-up"
            continue

        meta = pd.DataFrame(
            {
                "security_id": security.id,
                "symbol": security.symbol,
                "trade_date": combined.index.date,
                "close": prices.loc[combined.index, "close"].values,
            },
            index=combined.index,
        )
        frames_X.append(combined.drop(columns=["label"]))
        frames_y.append(combined["label"])
        frames_meta.append(meta)

    if not frames_X:
        raise InsufficientDataError(
            f"no security had enough history for a {horizon_days}d horizon; {skipped}"
        )

    X = pd.concat(frames_X)
    y = pd.concat(frames_y)
    meta = pd.concat(frames_meta)

    # Pooled cross-sectional data must be ordered by time for the walk-forward
    # splitter; ties broken by security keep the ordering deterministic.
    order = np.lexsort((meta["security_id"].values, meta["trade_date"].values))
    X, y, meta = X.iloc[order], y.iloc[order], meta.iloc[order]

    if skipped:
        log.info("dataset_securities_skipped", count=len(skipped), detail=skipped)

    return Dataset(
        X=X.reset_index(drop=True),
        y=y.reset_index(drop=True),
        meta=meta.reset_index(drop=True),
        horizon_days=horizon_days,
        feature_set_version=FEATURE_SET_VERSION,
        target_kind=kind,
    )


def build_inference_frame(
    db: Session, security: Security, *, as_of: date | None = None,
    config: FeatureConfig | None = None,
) -> tuple[pd.DataFrame, date, float]:
    """Latest feature row for live prediction.

    Returns (one-row frame, its trade date, the close used as reference). This
    row deliberately has no label -- it is the row the model is asked about.
    """
    config = config or FeatureConfig()
    prices = load_price_frame(db, security.id, end=as_of)
    if len(prices) < config.min_rows:
        raise InsufficientDataError(
            f"{security.symbol}: {len(prices)} bars available, {config.min_rows} required"
        )

    features = compute_features(prices, config)
    usable = features.dropna()
    if usable.empty:
        raise InsufficientDataError(f"{security.symbol}: no complete feature row available")

    last = usable.iloc[[-1]]
    trade_date = last.index[-1].date()
    return last, trade_date, float(prices.loc[last.index[-1], "close"])


def assert_no_leakage(dataset: Dataset, prices_by_security: dict[int, pd.Series]) -> None:
    """Re-derive every label from raw prices and confirm it matches.

    Guards against a future refactor silently changing the alignment.
    """
    mismatches = 0
    for i in range(dataset.n_rows):
        security_id = int(dataset.meta["security_id"].iloc[i])
        row_date = pd.Timestamp(dataset.meta["trade_date"].iloc[i])
        close = prices_by_security.get(security_id)
        if close is None or row_date not in close.index:
            continue

        position = close.index.get_loc(row_date)
        future = position + dataset.horizon_days
        if future >= len(close):
            raise AssertionError(
                f"row {i} has a label but no future bar exists -- leakage or bad drop"
            )
        expected_return = close.iloc[future] / close.iloc[position] - 1
        expected = (
            float(expected_return > 0)
            if dataset.target_kind == "classification"
            else float(expected_return)
        )
        if not np.isclose(expected, float(dataset.y.iloc[i]), atol=1e-9):
            mismatches += 1

    if mismatches:
        raise AssertionError(f"{mismatches} labels do not match a forward-looking return")
