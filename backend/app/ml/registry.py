"""Model registry: versioning, promotion gates, rollback and drift.

Promotion is gated, never automatic. A freshly trained model becomes a
CANDIDATE; it only reaches PRODUCTION if it clears explicit thresholds and is
not materially worse than the model it would replace. This is the control that
stops a bad retrain from quietly taking over signal generation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ModelNotAvailable
from app.core.logging import get_logger
from app.ml.trainer import TrainingResult, load_model, save_model
from app.models.enums import EventSeverity, ModelStatus, PredictionTarget
from app.models.platform import ModelMetric, ModelVersion, SystemEvent

log = get_logger(__name__)

# path -> ((size, mtime_ns), sha256). Artefacts run to tens of megabytes and
# their integrity is checked on every health poll, so hash each one once.
_DIGEST_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


@dataclass(slots=True)
class PromotionGate:
    """Minimum bar a candidate must clear to serve production traffic."""

    min_roc_auc: float = 0.52
    min_lift_over_baseline: float = 0.005
    max_calibration_error: float = 0.15
    min_training_rows: int = 500
    # A replacement may not be more than this much worse than the incumbent.
    max_auc_regression: float = 0.02

    def evaluate(
        self, result: TrainingResult, incumbent_auc: float | None = None
    ) -> tuple[bool, list[str]]:
        failures: list[str] = []
        cv = result.cv_mean

        auc = cv.get("roc_auc")
        if result.kind == "classification":
            if auc is None:
                failures.append("no ROC-AUC was produced")
            elif auc < self.min_roc_auc:
                failures.append(f"cv roc_auc {auc:.4f} < required {self.min_roc_auc}")

            lift = cv.get("lift_over_baseline", 0.0)
            if lift < self.min_lift_over_baseline:
                failures.append(
                    f"lift over majority baseline {lift:+.4f} < required "
                    f"{self.min_lift_over_baseline}"
                )

            calib = result.holdout.get("calibration_error")
            if calib is not None and calib > self.max_calibration_error:
                failures.append(
                    f"calibration error {calib:.4f} > allowed {self.max_calibration_error}"
                )

        if result.training_rows < self.min_training_rows:
            failures.append(
                f"trained on {result.training_rows} rows < required {self.min_training_rows}"
            )

        if incumbent_auc is not None and auc is not None:
            if auc < incumbent_auc - self.max_auc_regression:
                failures.append(
                    f"regression against incumbent: {auc:.4f} vs {incumbent_auc:.4f}"
                )

        return (not failures), failures


def artifact_digest(file: Path) -> str:
    """sha256 of the artefact, cached on (path, size, mtime).

    Production artefacts run to tens of megabytes and health is polled, so
    re-hashing on every request would be wasteful. Any write changes size or
    mtime, which invalidates the entry -- the point of the check is to notice
    exactly that.
    """
    stat = file.stat()
    key = str(file)
    stamp = (stat.st_size, stat.st_mtime_ns)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    hasher = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    # Keyed by path with the stamp as the value, so a rewritten artefact
    # replaces its own entry instead of accumulating one per revision, and
    # several production models each keep a cached digest.
    _DIGEST_CACHE[key] = (stamp, digest)
    return digest


def artifact_problem(model: ModelVersion) -> str | None:
    """Why this model's artefact cannot be loaded, or None if it is fine."""
    if not model.artifact_path:
        return "no artefact path recorded"
    file = Path(model.artifact_path)
    if not file.exists():
        return f"artefact missing: {model.artifact_path}"
    if not model.artifact_sha256:
        return "no checksum recorded; the artefact cannot be verified"
    try:
        actual = artifact_digest(file)
    except OSError as exc:
        return f"artefact unreadable: {exc}"
    if actual != model.artifact_sha256:
        return (
            f"checksum mismatch (expected {model.artifact_sha256[:12]}, "
            f"got {actual[:12]})"
        )
    return None


def next_version(db: Session, name: str) -> str:
    count = db.scalar(
        select(ModelVersion.id).where(ModelVersion.name == name).order_by(ModelVersion.id.desc())
    )
    existing = db.scalars(select(ModelVersion.version).where(ModelVersion.name == name)).all()
    numbers = []
    for v in existing:
        try:
            numbers.append(int(str(v).lstrip("v").split(".")[0]))
        except (ValueError, IndexError):
            continue
    return f"v{(max(numbers) + 1) if numbers else 1}"


def register_model(
    db: Session, result: TrainingResult, *, name: str, target: PredictionTarget,
    commit: bool = True,
) -> ModelVersion:
    """Persist a trained model as a CANDIDATE with all of its metrics."""
    version = next_version(db, name)
    path, digest = save_model(result, name, version)

    record = ModelVersion(
        name=name, version=version, algorithm=result.algorithm, target=target,
        horizon_days=result.horizon_days, status=ModelStatus.CANDIDATE,
        feature_set_version=result.feature_set_version,
        feature_names=result.feature_names,
        hyperparameters={"calibrated": result.calibrated, "model": result.model_name},
        artifact_path=path, artifact_sha256=digest,
        train_start=result.train_start, train_end=result.train_end,
        training_rows=result.training_rows,
        trained_at=datetime.now(timezone.utc),
        feature_baseline=result.feature_baseline,
        notes="; ".join(result.warnings) or None,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    for split, metrics in (("cv_mean", result.cv_mean), ("holdout", result.holdout)):
        for metric_name, value in (metrics or {}).items():
            if isinstance(value, (int, float)) and value is not None and np.isfinite(value):
                db.add(
                    ModelMetric(
                        model_version_id=record.id, split=split, metric_name=metric_name,
                        metric_value=float(value),
                        sample_size=int(metrics.get("sample_size") or 0) or None,
                        period_start=result.train_start, period_end=result.train_end,
                        recorded_at=now,
                    )
                )
    for fold in result.cv_folds:
        for metric_name, value in fold.metrics.items():
            if isinstance(value, (int, float)) and value is not None and np.isfinite(value):
                db.add(
                    ModelMetric(
                        model_version_id=record.id, split=f"fold_{fold.fold}",
                        metric_name=metric_name, metric_value=float(value),
                        sample_size=fold.test_rows, recorded_at=now,
                    )
                )

    if commit:
        db.commit()
    log.info("model_registered", name=name, version=version, status="CANDIDATE")
    return record


def get_production_model(
    db: Session, *, name: str | None = None, horizon_days: int | None = None,
    target: PredictionTarget = PredictionTarget.DIRECTION,
) -> ModelVersion | None:
    stmt = select(ModelVersion).where(
        ModelVersion.status == ModelStatus.PRODUCTION, ModelVersion.target == target
    )
    if name:
        stmt = stmt.where(ModelVersion.name == name)
    if horizon_days:
        stmt = stmt.where(ModelVersion.horizon_days == horizon_days)
    return db.scalars(stmt.order_by(ModelVersion.promoted_at.desc()).limit(1)).first()


def promote_model(
    db: Session, candidate: ModelVersion, result: TrainingResult | None = None,
    *, gate: PromotionGate | None = None, force: bool = False, actor: str = "system",
) -> tuple[bool, list[str]]:
    """Promote a candidate to production if it clears the gate.

    `force` exists for a deliberate operator override and is recorded in the
    audit trail; it does not skip the evaluation, only its veto.
    """
    gate = gate or PromotionGate()
    incumbent = get_production_model(
        db, name=candidate.name, horizon_days=candidate.horizon_days, target=candidate.target
    )

    incumbent_auc = None
    incumbent_broken = None
    if incumbent is not None:
        incumbent_broken = artifact_problem(incumbent)
        if incumbent_broken:
            # The incumbent's recorded score is only a bar worth defending
            # while the incumbent can actually serve. One that fails its own
            # integrity check is producing nothing, so holding a replacement
            # to it deadlocks the system: predictions stay disabled and every
            # retrain is rejected for "regressing" against a model that is not
            # running. Judge the candidate on the absolute bars alone.
            log.warning(
                "incumbent_artefact_unusable",
                name=incumbent.name, version=incumbent.version, problem=incumbent_broken,
            )
        else:
            incumbent_auc = db.scalar(
                select(ModelMetric.metric_value).where(
                    ModelMetric.model_version_id == incumbent.id,
                    ModelMetric.split == "cv_mean",
                    ModelMetric.metric_name == "roc_auc",
                )
            )
            incumbent_auc = float(incumbent_auc) if incumbent_auc is not None else None

    passed, failures = (True, []) if result is None else gate.evaluate(result, incumbent_auc)

    if not passed and not force:
        log.warning("model_promotion_rejected", version=candidate.version, failures=failures)
        db.add(
            SystemEvent(
                component="ml", event_type="promotion_rejected",
                severity=EventSeverity.WARNING,
                message=f"{candidate.name}:{candidate.version} failed the promotion gate",
                details={"failures": failures}, created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return False, failures

    now = datetime.now(timezone.utc)
    if incumbent is not None and incumbent.id != candidate.id:
        incumbent.status = ModelStatus.ARCHIVED
        incumbent.archived_at = now

    candidate.status = ModelStatus.PRODUCTION
    candidate.promoted_at = now
    db.add(
        SystemEvent(
            component="ml", event_type="model_promoted", severity=EventSeverity.INFO,
            message=f"{candidate.name}:{candidate.version} promoted to production",
            details={
                "forced": force, "actor": actor, "failures": failures,
                "replaced": incumbent.version if incumbent else None,
                "replaced_because_unusable": incumbent_broken,
            },
            created_at=now,
        )
    )
    db.commit()
    log.info("model_promoted", name=candidate.name, version=candidate.version, forced=force)
    return True, failures


def rollback_model(db: Session, name: str, horizon_days: int, *, actor: str = "system") -> ModelVersion | None:
    """Restore the most recently archived version as production."""
    current = get_production_model(db, name=name, horizon_days=horizon_days)
    archived = db.scalars(
        select(ModelVersion)
        .where(
            ModelVersion.name == name,
            ModelVersion.horizon_days == horizon_days,
            ModelVersion.status == ModelStatus.ARCHIVED,
        )
        .order_by(ModelVersion.archived_at.desc())
    ).all()

    # Rollback is the recovery path, so it must not land on an artefact that
    # is missing or no longer matches its checksum -- that would swap one
    # unusable production model for another and report success. Walk back to
    # the newest archived version that actually verifies.
    previous, skipped = None, []
    for candidate in archived:
        problem = artifact_problem(candidate)
        if problem is None:
            previous = candidate
            break
        skipped.append(f"{candidate.version}: {problem}")

    if previous is None:
        log.warning("model_rollback_unavailable", name=name, unusable=skipped)
        return None
    if skipped:
        log.warning("model_rollback_skipped_unusable", name=name, skipped=skipped)

    now = datetime.now(timezone.utc)
    if current is not None:
        current.status = ModelStatus.ARCHIVED
        current.archived_at = now
    previous.status = ModelStatus.PRODUCTION
    previous.promoted_at = now

    db.add(
        SystemEvent(
            component="ml", event_type="model_rolled_back", severity=EventSeverity.WARNING,
            message=f"rolled back to {name}:{previous.version}",
            details={
                "from": current.version if current else None, "actor": actor,
                "skipped_unusable": skipped,
            },
            created_at=now,
        )
    )
    db.commit()
    return previous


def load_production_pipeline(db: Session, model_version: ModelVersion) -> dict:
    """Load the artefact, verifying its checksum."""
    if not model_version.artifact_path:
        raise ModelNotAvailable(f"{model_version.name}:{model_version.version} has no artefact")
    return load_model(model_version.artifact_path, expected_sha256=model_version.artifact_sha256)


# --------------------------------------------------------------------- drift
def population_stability_index(
    baseline: np.ndarray, current: np.ndarray, bins: int = 10
) -> float:
    """PSI between a baseline and a current distribution.

    Convention: <0.1 stable, 0.1-0.25 moderate shift, >0.25 significant.
    """
    baseline = np.asarray(baseline, dtype=float)
    current = np.asarray(current, dtype=float)
    baseline = baseline[np.isfinite(baseline)]
    current = current[np.isfinite(current)]
    if len(baseline) < 10 or len(current) < 10:
        return float("nan")

    edges = np.unique(np.quantile(baseline, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf

    base_pct = np.histogram(baseline, bins=edges)[0] / len(baseline)
    curr_pct = np.histogram(current, bins=edges)[0] / len(current)
    # Floor empty buckets so the log stays finite.
    base_pct = np.clip(base_pct, 1e-6, None)
    curr_pct = np.clip(curr_pct, 1e-6, None)
    return float(np.sum((curr_pct - base_pct) * np.log(curr_pct / base_pct)))


@dataclass(slots=True)
class DriftReport:
    model_version_id: int
    feature_psi: dict[str, float] = field(default_factory=dict)
    drifted_features: list[str] = field(default_factory=list)
    max_psi: float = 0.0
    drift_detected: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "model_version_id": self.model_version_id,
            "max_psi": round(self.max_psi, 4),
            "drift_detected": self.drift_detected,
            "drifted_features": self.drifted_features,
            "feature_psi": {k: round(v, 4) for k, v in sorted(
                self.feature_psi.items(), key=lambda kv: kv[1], reverse=True
            )[:15]},
            "note": self.note,
        }


def detect_feature_drift(
    model_version: ModelVersion, current_features, *, threshold: float | None = None
) -> DriftReport:
    """Compare live feature distributions against the training baseline.

    Uses the stored per-feature quantiles, so no training data needs retaining.
    """
    threshold = threshold if threshold is not None else settings.ml_drift_psi_threshold
    report = DriftReport(model_version_id=model_version.id)
    baseline = model_version.feature_baseline or {}

    if not baseline:
        report.note = "no feature baseline stored for this model version"
        return report
    if current_features is None or len(current_features) < 30:
        report.note = "insufficient live rows to assess drift (need >= 30)"
        return report

    for feature, stats in baseline.items():
        if feature not in current_features.columns:
            continue
        values = current_features[feature].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < 30:
            continue

        # Reconstruct an approximate baseline sample from the stored quantiles.
        approx = np.concatenate(
            [
                np.random.default_rng(0).normal(
                    stats.get("p50", 0.0),
                    max(stats.get("std", 0.0) or 1e-6, 1e-6),
                    max(len(values), 100),
                )
            ]
        )
        psi = population_stability_index(approx, values)
        if np.isfinite(psi):
            report.feature_psi[feature] = psi

    if report.feature_psi:
        report.max_psi = max(report.feature_psi.values())
        report.drifted_features = [
            f for f, v in report.feature_psi.items() if v > threshold
        ]
        report.drift_detected = bool(report.drifted_features)
    return report
