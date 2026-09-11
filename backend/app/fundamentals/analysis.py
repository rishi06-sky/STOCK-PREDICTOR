"""Fundamental analysis and peer comparison.

Missing fundamentals stay missing. Every scoring function reports how much of
its input was actually available via `coverage`, so a score computed from two
of eight metrics is never presented with the same weight as a complete one.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.analysis import Fundamental
from app.models.market import Security

log = get_logger(__name__)

# Direction each metric should move for a healthier company.
HIGHER_IS_BETTER = {
    "roe", "roce", "revenue_growth", "profit_growth", "earnings_growth",
    "free_cash_flow", "dividend_yield", "gross_margin", "operating_margin",
    "net_margin", "eps",
}
LOWER_IS_BETTER = {"pe_ratio", "pb_ratio", "debt_to_equity"}

SCORED_METRICS = sorted(HIGHER_IS_BETTER | LOWER_IS_BETTER)


@dataclass(slots=True)
class PeerComparison:
    metric: str
    value: float | None
    peer_median: float | None
    percentile: float | None
    peer_count: int
    better_than_peers: bool | None


@dataclass(slots=True)
class FundamentalScore:
    security_id: int
    symbol: str
    score: float | None            # 0..100, None when nothing is available
    coverage: float                # fraction of metrics that had data
    metrics_used: list[str] = field(default_factory=list)
    metrics_missing: list[str] = field(default_factory=list)
    comparisons: list[PeerComparison] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        """Below half coverage the score is too thin to act on."""
        return self.score is not None and self.coverage >= 0.5

    def as_dict(self) -> dict:
        return {
            "security_id": self.security_id, "symbol": self.symbol,
            "score": self.score, "coverage": round(self.coverage, 3),
            "reliable": self.is_reliable,
            "metrics_used": self.metrics_used, "metrics_missing": self.metrics_missing,
            "notes": self.notes,
            "peer_comparisons": [
                {
                    "metric": c.metric, "value": c.value, "peer_median": c.peer_median,
                    "percentile": c.percentile, "peer_count": c.peer_count,
                    "better_than_peers": c.better_than_peers,
                }
                for c in self.comparisons
            ],
        }


def latest_fundamental(
    db: Session, security_id: int, *, as_of: date | None = None
) -> Fundamental | None:
    """Most recent fundamentals known at `as_of`.

    Filters on `reported_at` rather than `as_of_date`: a quarter's figures must
    not be visible before the date they were actually published, or every
    backtest built on them is contaminated.
    """
    stmt = select(Fundamental).where(Fundamental.security_id == security_id)
    if as_of is not None:
        stmt = stmt.where(
            (Fundamental.reported_at.is_(None) & (Fundamental.as_of_date <= as_of))
            | (Fundamental.reported_at <= as_of)
        )
    return db.scalars(stmt.order_by(Fundamental.as_of_date.desc()).limit(1)).first()


def _percentile(value: float, population: list[float], *, higher_is_better: bool) -> float:
    """Percentile rank of `value` within `population`, oriented so 100 is best."""
    if not population:
        return 50.0
    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    rank = (below + 0.5 * equal) / len(population) * 100
    return rank if higher_is_better else 100 - rank


def compare_with_peers(
    db: Session, security: Security, fundamental: Fundamental, *, as_of: date | None = None
) -> list[PeerComparison]:
    """Compare against same-sector, same-exchange companies."""
    if not security.sector:
        return []

    peer_ids = db.scalars(
        select(Security.id).where(
            Security.sector == security.sector,
            Security.exchange_id == security.exchange_id,
            Security.id != security.id,
            Security.is_active.is_(True),
        )
    ).all()
    if not peer_ids:
        return []

    peer_rows = [
        row for pid in peer_ids
        if (row := latest_fundamental(db, pid, as_of=as_of)) is not None
    ]

    comparisons: list[PeerComparison] = []
    for metric in SCORED_METRICS:
        value = getattr(fundamental, metric, None)
        values = [
            float(v) for row in peer_rows
            if (v := getattr(row, metric, None)) is not None
        ]
        if value is None:
            comparisons.append(PeerComparison(metric, None, None, None, len(values), None))
            continue
        value = float(value)
        if not values:
            comparisons.append(PeerComparison(metric, value, None, None, 0, None))
            continue
        higher = metric in HIGHER_IS_BETTER
        median = statistics.median(values)
        comparisons.append(
            PeerComparison(
                metric=metric, value=value, peer_median=median,
                percentile=round(_percentile(value, values, higher_is_better=higher), 2),
                peer_count=len(values),
                better_than_peers=(value > median) if higher else (value < median),
            )
        )
    return comparisons


def score_fundamentals(
    db: Session, security: Security, *, as_of: date | None = None
) -> FundamentalScore:
    """Blend peer percentiles into a 0-100 quality score."""
    fundamental = latest_fundamental(db, security.id, as_of=as_of)
    if fundamental is None:
        return FundamentalScore(
            security_id=security.id, symbol=security.symbol, score=None, coverage=0.0,
            metrics_missing=SCORED_METRICS,
            notes=["no fundamentals available for this security"],
        )

    comparisons = compare_with_peers(db, security, fundamental, as_of=as_of)
    by_metric = {c.metric: c for c in comparisons}

    used, missing, percentiles, notes = [], [], [], []
    for metric in SCORED_METRICS:
        value = getattr(fundamental, metric, None)
        if value is None:
            missing.append(metric)
            continue
        value = float(value)
        # A negative P/E is a loss-making company, not a cheap one; ranking it
        # on "lower is better" would reward losses.
        if metric in ("pe_ratio", "pb_ratio") and value <= 0:
            notes.append(f"{metric} is non-positive ({value:.2f}); excluded from scoring")
            missing.append(metric)
            continue
        used.append(metric)
        comparison = by_metric.get(metric)
        if comparison and comparison.percentile is not None:
            percentiles.append(comparison.percentile)
        else:
            # No peers to rank against: contribute neutrally rather than guess.
            percentiles.append(50.0)
            notes.append(f"{metric}: no peer data, scored neutral")

    coverage = len(used) / len(SCORED_METRICS)
    score = round(sum(percentiles) / len(percentiles), 2) if percentiles else None
    if score is not None and coverage < 0.5:
        notes.append(f"low coverage ({coverage:.0%}); treat this score as indicative only")

    return FundamentalScore(
        security_id=security.id, symbol=security.symbol, score=score, coverage=coverage,
        metrics_used=used, metrics_missing=missing, comparisons=comparisons, notes=notes,
    )


def derive_ratios(fundamental: Fundamental) -> dict[str, float | None]:
    """Ratios computable from stored absolutes, where the inputs exist."""
    out: dict[str, float | None] = {}
    revenue = float(fundamental.revenue) if fundamental.revenue is not None else None
    net_income = float(fundamental.net_income) if fundamental.net_income is not None else None
    fcf = float(fundamental.free_cash_flow) if fundamental.free_cash_flow is not None else None

    out["net_margin_derived"] = (
        net_income / revenue if revenue not in (None, 0) and net_income is not None else None
    )
    out["fcf_margin"] = (
        fcf / revenue if revenue not in (None, 0) and fcf is not None else None
    )
    out["earnings_yield"] = (
        1 / float(fundamental.pe_ratio)
        if fundamental.pe_ratio not in (None, 0) and float(fundamental.pe_ratio) > 0
        else None
    )
    return out
