"""Live prediction service.

Every prediction is refused rather than guessed when any precondition fails:
no production model, stale price data, insufficient history, or a feature
vector the model was not trained on. A missing prediction is recoverable; a
confidently wrong one is not.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    InsufficientDataError, ModelNotAvailable, StaleDataError,
)
from app.core.logging import get_logger
from app.ml.dataset import build_inference_frame
from app.ml.registry import get_production_model, load_production_pipeline
from app.models.analysis import Prediction
from app.models.enums import DataQuality, PredictionTarget
from app.models.market import PriceData, Quote, Security
from app.models.platform import ModelVersion
from app.technical.features import FEATURE_SET_VERSION

log = get_logger(__name__)


@dataclass(slots=True)
class PredictionResult:
    security_id: int
    symbol: str
    as_of_date: date
    horizon_days: int
    probability: float
    reference_price: float
    model_version_id: int
    model_version: str
    data_quality: DataQuality
    price_as_of: datetime
    expected_return: float | None = None
    feature_snapshot: dict = field(default_factory=dict)
    calibrated: bool = False

    def as_dict(self) -> dict:
        return {
            "security_id": self.security_id, "symbol": self.symbol,
            "as_of_date": str(self.as_of_date), "horizon_days": self.horizon_days,
            "probability": round(self.probability, 6),
            "expected_return": (
                round(self.expected_return, 6) if self.expected_return is not None else None
            ),
            "reference_price": round(self.reference_price, 4),
            "model_version": self.model_version, "calibrated": self.calibrated,
            "data_quality": str(self.data_quality),
            "price_as_of": self.price_as_of.isoformat(),
        }


class _PipelineCache:
    """Keeps loaded artefacts in memory, keyed by version id and checksum."""

    def __init__(self):
        self._entries: dict[int, tuple[str, dict]] = {}
        self._lock = threading.Lock()

    def get(self, model_version: ModelVersion, loader) -> dict:
        key = model_version.id
        digest = model_version.artifact_sha256 or ""
        with self._lock:
            cached = self._entries.get(key)
            if cached and cached[0] == digest:
                return cached[1]
        bundle = loader()
        with self._lock:
            self._entries[key] = (digest, bundle)
        return bundle

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _PipelineCache()


def clear_pipeline_cache() -> None:
    _cache.clear()


class PredictionService:
    def __init__(self, db: Session):
        self.db = db

    def _load(self, model_version: ModelVersion) -> dict:
        return _cache.get(
            model_version, lambda: load_production_pipeline(self.db, model_version)
        )

    def check_data_freshness(self, security: Security) -> tuple[DataQuality, datetime]:
        """Return the freshness of the newest usable price for this security.

        Raises StaleDataError when nothing recent enough exists -- the caller
        must then decline to predict rather than use an old close.
        """
        quote = self.db.get(Quote, security.id)
        now = datetime.now(timezone.utc)

        if quote is not None:
            age = (now - quote.source_timestamp).total_seconds()
            if quote.quality is DataQuality.SYNTHETIC:
                return DataQuality.SYNTHETIC, quote.source_timestamp
            if age <= settings.quote_staleness_seconds:
                return quote.quality, quote.source_timestamp

        latest = self.db.scalars(
            select(PriceData)
            .where(PriceData.security_id == security.id)
            .order_by(PriceData.trade_date.desc())
            .limit(1)
        ).first()
        if latest is None:
            raise InsufficientDataError(f"{security.symbol}: no price history stored")

        age_hours = (now - latest.source_timestamp).total_seconds() / 3600
        if latest.quality is DataQuality.SYNTHETIC:
            return DataQuality.SYNTHETIC, latest.source_timestamp
        if age_hours > settings.eod_staleness_hours:
            raise StaleDataError(
                security.symbol, age_hours * 3600, settings.eod_staleness_hours * 3600
            )
        return DataQuality.EOD, latest.source_timestamp

    def predict(
        self, security: Security, *, horizon_days: int, as_of: date | None = None,
        model_name: str | None = None, require_fresh: bool = True,
    ) -> PredictionResult:
        model_version = get_production_model(
            self.db, name=model_name, horizon_days=horizon_days,
            target=PredictionTarget.DIRECTION,
        )
        if model_version is None:
            raise ModelNotAvailable(
                f"no production model for a {horizon_days}-day horizon"
            )

        if model_version.feature_set_version != FEATURE_SET_VERSION:
            raise ModelNotAvailable(
                f"{model_version.name}:{model_version.version} was trained on feature set "
                f"{model_version.feature_set_version}, but this build produces "
                f"{FEATURE_SET_VERSION}; retrain before serving predictions"
            )

        quality, price_as_of = (
            self.check_data_freshness(security)
            if require_fresh
            else (DataQuality.EOD, datetime.now(timezone.utc))
        )

        frame, trade_date, reference_price = build_inference_frame(
            self.db, security, as_of=as_of
        )
        bundle = self._load(model_version)
        pipeline = bundle["pipeline"]
        expected_features = bundle.get("feature_names") or list(frame.columns)

        missing = [f for f in expected_features if f not in frame.columns]
        if missing:
            raise ModelNotAvailable(
                f"feature vector is missing {len(missing)} column(s) the model expects: "
                f"{missing[:5]}"
            )
        # Reindex to the exact training column order; a silent reorder would
        # feed each feature into the wrong coefficient.
        frame = frame[expected_features]

        probability = float(pipeline.predict_proba(frame)[0, 1])
        if not np.isfinite(probability):
            raise ModelNotAvailable("model produced a non-finite probability")

        return PredictionResult(
            security_id=security.id, symbol=security.symbol, as_of_date=trade_date,
            horizon_days=horizon_days, probability=probability,
            reference_price=reference_price,
            model_version_id=model_version.id, model_version=model_version.version,
            data_quality=quality, price_as_of=price_as_of,
            feature_snapshot={
                k: (None if pd.isna(v) else round(float(v), 6))
                for k, v in frame.iloc[0].items()
            },
            calibrated=bool((model_version.hyperparameters or {}).get("calibrated")),
        )

    def persist(self, result: PredictionResult, *, commit: bool = True) -> None:
        stmt = pg_insert(Prediction).values(
            security_id=result.security_id,
            model_version_id=result.model_version_id,
            as_of_date=result.as_of_date,
            horizon_days=result.horizon_days,
            target=PredictionTarget.DIRECTION,
            raw_probability=result.probability,
            calibrated_probability=result.probability if result.calibrated else None,
            expected_return=result.expected_return,
            feature_snapshot=result.feature_snapshot,
            reference_price=result.reference_price,
            data_quality=result.data_quality,
            created_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_predictions_sec_model_date_horizon",
            set_={
                "raw_probability": stmt.excluded.raw_probability,
                "calibrated_probability": stmt.excluded.calibrated_probability,
                "expected_return": stmt.excluded.expected_return,
                "feature_snapshot": stmt.excluded.feature_snapshot,
                "reference_price": stmt.excluded.reference_price,
                "data_quality": stmt.excluded.data_quality,
                "created_at": stmt.excluded.created_at,
            },
        )
        self.db.execute(stmt)
        if commit:
            self.db.commit()

    def predict_universe(
        self, securities: list[Security], *, horizon_days: int, persist: bool = True,
    ) -> tuple[list[PredictionResult], dict[str, str]]:
        """Predict across a universe. Failures are reported, never fabricated."""
        results: list[PredictionResult] = []
        failures: dict[str, str] = {}

        for security in securities:
            try:
                result = self.predict(security, horizon_days=horizon_days)
            except (ModelNotAvailable, StaleDataError, InsufficientDataError) as exc:
                failures[security.symbol] = str(exc)
                continue
            except Exception as exc:
                failures[security.symbol] = f"unexpected: {exc}"
                log.error("prediction_failed", symbol=security.symbol, error=str(exc))
                continue

            results.append(result)
            if persist:
                self.persist(result, commit=False)

        if persist:
            self.db.commit()
        return results, failures


def estimate_expected_return(
    probability: float, horizon_days: int, volatility: float | None
) -> float:
    """Translate a direction probability into an expected return.

    Derived from the model's edge over a coin flip, scaled by the security's
    own volatility over the horizon. This is an estimate with wide error bars,
    not a price target -- the signal engine presents it as such.
    """
    edge = (probability - 0.5) * 2          # -1 .. 1
    horizon_vol = (volatility or 0.25) * np.sqrt(horizon_days / 252)
    # Cap at a modest fraction of a standard deviation; claiming more would
    # imply a precision the model does not have.
    return float(np.clip(edge * horizon_vol * 0.5, -0.25, 0.25))
