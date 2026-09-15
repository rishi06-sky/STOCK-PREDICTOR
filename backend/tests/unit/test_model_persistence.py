"""Model artefacts live on disk; the registry only holds a path and a checksum.

That split is where a deployed model can quietly die: if anything overwrites an
artefact a live ModelVersion row still points at, the recorded checksum stops
matching and every prediction fails with a message about hashes rather than
about what actually happened. These tests pin down both halves of the guard --
the write side must refuse to clobber, and the read side must refuse to load.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from app.core.config import settings
from app.ml.trainer import TrainingResult, load_model, model_store_dir, save_model


def _fitted_result(*, model_name: str = "logistic_regression") -> TrainingResult:
    """A minimal but genuinely fitted pipeline -- enough to serialise."""
    rng = np.random.default_rng(11)
    x = rng.normal(size=(60, 2))
    y = (x[:, 0] + rng.normal(scale=0.5, size=60) > 0).astype(int)
    pipeline = Pipeline([("clf", LogisticRegression(max_iter=200))]).fit(x, y)
    return TrainingResult(
        model_name=model_name,
        algorithm="LogisticRegression",
        kind="classification",
        horizon_days=5,
        feature_set_version="v1",
        feature_names=["f0", "f1"],
        pipeline=pipeline,
        training_rows=60,
    )


class TestModelStoreIsolation:
    def test_tests_never_write_to_the_development_store(self):
        """The suite trains models; none of them may land in ./model_store.

        Version numbers are allocated from the registry table, which is empty
        in the test database, so every test that registers a model asks for
        "v1" -- the same filename the development registry is using.
        """
        store = Path(settings.ml_model_dir).resolve()
        assert store != Path("./model_store").resolve(), (
            "tests are pointed at the development model store; a training test "
            "would overwrite deployed artefacts"
        )


class TestSaveModel:
    def test_writes_artefact_and_returns_matching_digest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")

        file = Path(path)
        assert file.exists()
        assert digest == hashlib.sha256(file.read_bytes()).hexdigest()
        assert (file.parent / "v1.metrics.json").exists()

    def test_refuses_to_overwrite_an_existing_artefact(self, tmp_path, monkeypatch):
        """The bug this guards: a silently clobbered live model.

        Deleting the registry row, restoring an older database, or running a
        second process against the same store all put the allocator back on a
        version that is already on disk. Overwriting leaves the registry
        pointing at bytes it has never seen, and predictions fail later with a
        checksum error that says nothing about the cause.
        """
        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")
        original = Path(path).read_bytes()

        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            save_model(_fitted_result(model_name="random_forest"), "direction_5d", "v1")

        # The refusal must be total: the first artefact is still intact and
        # still matches the checksum its registry row would carry.
        assert Path(path).read_bytes() == original
        assert hashlib.sha256(original).hexdigest() == digest

    def test_model_store_dir_follows_the_configured_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path / "nested"))
        assert model_store_dir() == tmp_path / "nested"
        assert (tmp_path / "nested").is_dir()


class TestLoadModel:
    def test_round_trips_a_saved_pipeline(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")

        payload = load_model(path, expected_sha256=digest)
        assert payload["feature_names"] == ["f0", "f1"]
        assert payload["horizon_days"] == 5
        assert payload["pipeline"].predict_proba(np.zeros((1, 2))).shape == (1, 2)

    def test_rejects_an_artefact_whose_bytes_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")

        Path(path).write_bytes(Path(path).read_bytes() + b"tampered")
        with pytest.raises(ValueError, match="checksum mismatch"):
            load_model(path, expected_sha256=digest)

    def test_reports_a_missing_artefact_as_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_model(str(tmp_path / "absent.joblib"), expected_sha256=None)


class TestArtefactHealth:
    """The registry row is not the model; the artefact is.

    A production row pointing at a missing or changed artefact means every
    prediction is already failing. Health must say so rather than counting
    rows and reporting HEALTHY.
    """

    @staticmethod
    def _row(path: str | None, digest: str | None):
        from app.models.platform import ModelVersion

        return ModelVersion(
            name="direction_5d", version="v1", artifact_path=path,
            artifact_sha256=digest,
        )

    def test_verified_artefact_reports_no_problem(self, tmp_path, monkeypatch):
        from app.ml.registry import artifact_problem

        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")
        assert artifact_problem(self._row(path, digest)) is None

    def test_missing_artefact_is_a_problem(self, tmp_path):
        from app.ml.registry import artifact_problem

        problem = artifact_problem(self._row(str(tmp_path / "gone.joblib"), "a" * 64))
        assert problem is not None and "missing" in problem

    def test_changed_artefact_is_a_problem(self, tmp_path, monkeypatch):
        """The exact failure this was written for: bytes replaced underneath a
        live registry row, which the old check reported as HEALTHY."""
        from app.ml.registry import artifact_problem

        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, digest = save_model(_fitted_result(), "direction_5d", "v1")
        assert artifact_problem(self._row(path, digest)) is None

        Path(path).write_bytes(Path(path).read_bytes() + b"rewritten")
        problem = artifact_problem(self._row(path, digest))
        assert problem is not None and "checksum mismatch" in problem

    def test_unrecorded_checksum_is_a_problem(self, tmp_path, monkeypatch):
        from app.ml.registry import artifact_problem

        monkeypatch.setattr(settings, "ml_model_dir", str(tmp_path))
        path, _ = save_model(_fitted_result(), "direction_5d", "v1")
        problem = artifact_problem(self._row(path, None))
        assert problem is not None and "cannot be verified" in problem

    def test_absent_path_is_a_problem(self):
        from app.ml.registry import artifact_problem

        problem = artifact_problem(self._row(None, None))
        assert problem is not None and "no artefact path" in problem
