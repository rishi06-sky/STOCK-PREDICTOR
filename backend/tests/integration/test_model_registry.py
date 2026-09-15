"""Promotion and rollback when the model store and the registry disagree.

A ModelVersion row is a pointer: a path plus a checksum. If the artefact it
points at goes missing or changes, the row still looks perfectly healthy in
the database while predictions fail on every security. These tests cover what
the registry must do once that has happened -- which is the situation an
operator is actually in when they come to fix it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from app.core.config import settings
from app.ml.registry import (
    PromotionGate,
    promote_model,
    register_model,
    rollback_model,
)
from app.ml.trainer import TrainingResult
from app.models.enums import ModelStatus, PredictionTarget
from app.models.platform import ModelVersion
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

NAME = "direction_5d"


def _result(*, auc: float, rows: int = 20_000) -> TrainingResult:
    """A training result that clears every absolute bar at the given AUC."""
    rng = np.random.default_rng(3)
    x = rng.normal(size=(80, 2))
    y = (x[:, 0] > 0).astype(int)
    pipeline = Pipeline([("clf", LogisticRegression(max_iter=200))]).fit(x, y)
    return TrainingResult(
        model_name="logistic_regression",
        algorithm="LogisticRegression",
        kind="classification",
        horizon_days=5,
        feature_set_version="v1",
        feature_names=["f0", "f1"],
        cv_mean={"roc_auc": auc, "lift_over_baseline": 0.05},
        holdout={"roc_auc": auc, "calibration_error": 0.02},
        pipeline=pipeline,
        training_rows=rows,
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
    return tmp_path


def _register(db, result: TrainingResult) -> ModelVersion:
    version = register_model(
        db, result, name=NAME, target=PredictionTarget.DIRECTION, commit=False
    )
    db.flush()
    return version


def _corrupt(version: ModelVersion) -> None:
    """Replace the artefact's bytes, exactly as an errant writer would."""
    Path(version.artifact_path).write_bytes(b"not the model that was registered")


class TestPromotionAgainstABrokenIncumbent:
    def test_a_healthy_incumbent_still_blocks_a_worse_candidate(self, db, store):
        strong = _register(db, _result(auc=0.64))
        assert promote_model(db, strong, _result(auc=0.64), gate=PromotionGate())[0]

        weak = _register(db, _result(auc=0.58))
        promoted, failures = promote_model(db, weak, _result(auc=0.58), gate=PromotionGate())

        assert not promoted
        assert any("regression against incumbent" in f for f in failures)
        assert strong.status is ModelStatus.PRODUCTION

    def test_an_unusable_incumbent_does_not_block_a_replacement(self, db, store):
        """The deadlock this guards against.

        Production is pinned to an artefact that will not load, so nothing is
        being served. Judging the replacement against that model's recorded
        score rejects every retrain for "regressing" -- and predictions stay
        disabled indefinitely.
        """
        broken = _register(db, _result(auc=0.64))
        assert promote_model(db, broken, _result(auc=0.64), gate=PromotionGate())[0]
        _corrupt(broken)

        replacement = _register(db, _result(auc=0.58))
        promoted, failures = promote_model(
            db, replacement, _result(auc=0.58), gate=PromotionGate()
        )

        assert promoted, f"replacement for an unusable model was refused: {failures}"
        assert replacement.status is ModelStatus.PRODUCTION
        assert broken.status is ModelStatus.ARCHIVED

    def test_the_absolute_bars_still_apply_when_the_incumbent_is_broken(self, db, store):
        """Ignoring the incumbent must not turn into promoting anything at all."""
        broken = _register(db, _result(auc=0.64))
        assert promote_model(db, broken, _result(auc=0.64), gate=PromotionGate())[0]
        _corrupt(broken)

        no_edge = _result(auc=0.51)
        no_edge.cv_mean["lift_over_baseline"] = 0.0
        candidate = _register(db, no_edge)
        promoted, failures = promote_model(db, candidate, no_edge, gate=PromotionGate())

        assert not promoted
        assert any("roc_auc" in f for f in failures)
        assert candidate.status is ModelStatus.CANDIDATE


class TestRollback:
    def test_rolls_back_to_the_most_recent_archived_version(self, db, store):
        first = _register(db, _result(auc=0.64))
        promote_model(db, first, _result(auc=0.64), gate=PromotionGate())
        second = _register(db, _result(auc=0.65))
        promote_model(db, second, _result(auc=0.65), gate=PromotionGate())

        restored = rollback_model(db, NAME, horizon_days=5)

        assert restored is not None and restored.id == first.id
        assert first.status is ModelStatus.PRODUCTION
        assert second.status is ModelStatus.ARCHIVED

    def test_skips_an_archived_version_whose_artefact_is_unusable(self, db, store):
        """Rollback is the recovery path; it must not restore a broken model.

        Otherwise recovery reports success while swapping one unusable
        production model for another.
        """
        oldest = _register(db, _result(auc=0.64))
        promote_model(db, oldest, _result(auc=0.64), gate=PromotionGate())
        middle = _register(db, _result(auc=0.65))
        promote_model(db, middle, _result(auc=0.65), gate=PromotionGate())
        newest = _register(db, _result(auc=0.66))
        promote_model(db, newest, _result(auc=0.66), gate=PromotionGate())

        # The obvious rollback target is the one that is broken.
        _corrupt(middle)
        # archived_at is written with the same clock for both, so order it
        # explicitly rather than relying on tie-breaking.
        now = datetime.now(timezone.utc)
        oldest.archived_at = now - timedelta(minutes=2)
        middle.archived_at = now - timedelta(minutes=1)
        db.flush()

        restored = rollback_model(db, NAME, horizon_days=5)

        assert restored is not None
        assert restored.id == oldest.id, "rollback landed on the unusable artefact"
        assert middle.status is ModelStatus.ARCHIVED

    def test_returns_none_when_no_archived_version_is_usable(self, db, store):
        first = _register(db, _result(auc=0.64))
        promote_model(db, first, _result(auc=0.64), gate=PromotionGate())
        second = _register(db, _result(auc=0.65))
        promote_model(db, second, _result(auc=0.65), gate=PromotionGate())
        _corrupt(first)

        assert rollback_model(db, NAME, horizon_days=5) is None
        assert second.status is ModelStatus.PRODUCTION


class TestMeasuredEdge:
    """Signal confidence is scaled by the model's measured edge.

    Walk-forward CV and the held-out block routinely disagree. Taking the
    higher of the two would present an edge the out-of-sample test does not
    support, and the confidence floor that keeps weak models from producing
    actionable signals would stop doing its job.
    """

    @staticmethod
    def _metric(version, split: str, value: float):
        from app.models.platform import ModelMetric

        return ModelMetric(
            model_version_id=version.id, split=split, metric_name="roc_auc",
            metric_value=value, recorded_at=datetime.now(timezone.utc),
        )

    def _edge(self, db, **splits: float) -> float | None:
        """The edge reported for a model whose only metrics are `splits`."""
        from app.models.platform import ModelMetric
        from app.services.pipeline import Pipeline

        version = _register(db, _result(auc=0.64))
        # register_model records the training result's own metrics; clear them
        # so each case asserts against exactly the splits it sets.
        db.query(ModelMetric).filter_by(model_version_id=version.id).delete()
        for split, value in splits.items():
            db.add(self._metric(version, split, value))
        db.flush()
        return Pipeline(db).measured_edge(version)

    def test_prefers_the_weaker_holdout_over_a_flattering_cv(self, db, store):
        assert self._edge(db, cv_mean=0.601, holdout=0.515) == pytest.approx(0.515)

    def test_prefers_the_weaker_cv_over_a_flattering_holdout(self, db, store):
        """A single lucky holdout block must not raise the quoted edge either."""
        assert self._edge(db, cv_mean=0.540, holdout=0.680) == pytest.approx(0.540)

    def test_falls_back_to_whichever_split_was_recorded(self, db, store):
        assert self._edge(db, cv_mean=0.572) == pytest.approx(0.572)

    def test_reports_no_edge_when_nothing_was_measured(self, db, store):
        from app.models.platform import ModelMetric
        from app.services.pipeline import Pipeline

        version = _register(db, _result(auc=0.64))
        db.query(ModelMetric).filter_by(model_version_id=version.id).delete()
        db.flush()
        # None, not 0.5: an unmeasured model is unknown, and the signal engine
        # treats unknown as untrustworthy rather than as a coin flip.
        assert Pipeline(db).measured_edge(version) is None

    def test_ignores_splits_that_are_not_out_of_sample(self, db, store):
        """In-sample training scores are always flattering and never the edge."""
        assert self._edge(db, cv_mean=0.601, holdout=0.560, train=0.990) == pytest.approx(
            0.560
        )
