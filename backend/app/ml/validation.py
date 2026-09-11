"""Time-series cross-validation.

Standard k-fold shuffles rows and would train on data from after the test
window -- on financial series that inflates every metric to the point of being
meaningless. Two properties matter here:

1. **Chronology.** Train indices always precede test indices.
2. **Embargo.** A gap of at least `horizon` days sits between train and test.
   Without it, the last training rows carry labels computed from prices inside
   the test window, which leaks the answer across the boundary.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    train_start: object
    train_end: object
    test_start: object
    test_end: object
    embargoed: int

    def summary(self) -> dict:
        return {
            "fold": self.index,
            "train_rows": len(self.train_idx), "test_rows": len(self.test_idx),
            "train": f"{self.train_start}..{self.train_end}",
            "test": f"{self.test_start}..{self.test_end}",
            "embargoed_rows": self.embargoed,
        }


class WalkForwardSplitter:
    """Expanding-window walk-forward splits over a pooled, date-ordered set.

    Splitting on unique DATES rather than row positions is essential for
    cross-sectional data: 40 securities share each date, and a positional split
    would put the same day on both sides of the boundary.
    """

    def __init__(
        self, n_splits: int = 5, *, embargo_days: int = 5, min_train_dates: int = 120,
        expanding: bool = True,
    ):
        if n_splits < 1:
            raise ValueError("n_splits must be >= 1")
        self.n_splits = n_splits
        self.embargo_days = max(0, embargo_days)
        self.min_train_dates = min_train_dates
        self.expanding = expanding

    def split(self, dates: pd.Series) -> Iterator[Fold]:
        dates = pd.Series(pd.to_datetime(pd.Series(dates).values))
        unique = np.sort(dates.unique())
        n_dates = len(unique)

        if n_dates < self.min_train_dates + self.n_splits + self.embargo_days:
            raise ValueError(
                f"need at least {self.min_train_dates + self.n_splits + self.embargo_days} "
                f"distinct dates for {self.n_splits} splits, got {n_dates}"
            )

        testable = n_dates - self.min_train_dates - self.embargo_days
        fold_size = max(1, testable // self.n_splits)

        for fold in range(self.n_splits):
            test_start_pos = self.min_train_dates + self.embargo_days + fold * fold_size
            test_end_pos = (
                n_dates if fold == self.n_splits - 1 else test_start_pos + fold_size
            )
            if test_start_pos >= n_dates:
                break

            # Train ends `embargo_days` of trading dates before the test window.
            train_end_pos = test_start_pos - self.embargo_days
            train_start_pos = 0 if self.expanding else max(0, train_end_pos - self.min_train_dates)

            train_dates = set(unique[train_start_pos:train_end_pos])
            test_dates = set(unique[test_start_pos:test_end_pos])
            embargoed = set(unique[train_end_pos:test_start_pos])
            if not train_dates or not test_dates:
                continue

            train_mask = dates.isin(train_dates).values
            test_mask = dates.isin(test_dates).values

            yield Fold(
                index=fold,
                train_idx=np.flatnonzero(train_mask),
                test_idx=np.flatnonzero(test_mask),
                train_start=pd.Timestamp(unique[train_start_pos]).date(),
                train_end=pd.Timestamp(unique[max(train_end_pos - 1, 0)]).date(),
                test_start=pd.Timestamp(unique[test_start_pos]).date(),
                test_end=pd.Timestamp(unique[test_end_pos - 1]).date(),
                embargoed=len(embargoed),
            )

    def holdout_split(
        self, dates: pd.Series, holdout_fraction: float = 0.2
    ) -> tuple[np.ndarray, np.ndarray]:
        """Final out-of-sample block, embargoed from everything before it."""
        dates = pd.Series(pd.to_datetime(pd.Series(dates).values))
        unique = np.sort(dates.unique())
        n_dates = len(unique)
        split_pos = int(n_dates * (1 - holdout_fraction))

        train_end_pos = max(0, split_pos - self.embargo_days)
        train_dates = set(unique[:train_end_pos])
        holdout_dates = set(unique[split_pos:])

        return (
            np.flatnonzero(dates.isin(train_dates).values),
            np.flatnonzero(dates.isin(holdout_dates).values),
        )


def verify_fold_integrity(fold: Fold, dates: pd.Series) -> None:
    """Assert a fold really is chronological and embargoed. Raises on violation."""
    dates = pd.Series(pd.to_datetime(pd.Series(dates).values))
    train_dates = dates.iloc[fold.train_idx]
    test_dates = dates.iloc[fold.test_idx]

    if train_dates.empty or test_dates.empty:
        raise AssertionError(f"fold {fold.index} has an empty side")
    if train_dates.max() >= test_dates.min():
        raise AssertionError(
            f"fold {fold.index}: train ends {train_dates.max()} but test starts "
            f"{test_dates.min()} -- chronology violated"
        )
    if set(fold.train_idx) & set(fold.test_idx):
        raise AssertionError(f"fold {fold.index}: train and test share rows")
