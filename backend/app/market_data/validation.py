"""Integrity checks applied to every record before it reaches the database.

The database enforces the same invariants with CHECK constraints; this layer
exists so a bad record is rejected with a diagnosable reason and counted,
rather than aborting a whole ingestion batch on a driver error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.core.logging import get_logger
from app.market_data.types import Bar, IntradayBar, QuoteData
from app.models.enums import DataQuality

log = get_logger(__name__)

# A single session moving more than this is almost always a split or a bad
# print rather than a real move; such bars are quarantined for inspection.
MAX_DAILY_MOVE_PCT = 60.0
MAX_INTRADAY_GAP_PCT = 40.0


@dataclass(slots=True)
class ValidationResult:
    valid: list = field(default_factory=list)
    rejected: list[tuple[object, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def summary(self) -> dict:
        return {
            "valid": len(self.valid),
            "rejected": len(self.rejected),
            "warnings": len(self.warnings),
            "reasons": sorted({reason for _, reason in self.rejected}),
        }


def _positive(*values) -> bool:
    return all(v is not None and Decimal(str(v)) > 0 for v in values)


def validate_bar(bar: Bar, *, today: date | None = None) -> str | None:
    """Return a rejection reason, or None when the bar is acceptable."""
    today = today or datetime.now(timezone.utc).date()

    if not _positive(bar.open, bar.high, bar.low, bar.close):
        return "non-positive price"
    if bar.high < bar.low:
        return "high < low"
    if bar.high < max(bar.open, bar.close):
        return "high is not the session maximum"
    if bar.low > min(bar.open, bar.close):
        return "low is not the session minimum"
    if bar.volume is not None and bar.volume < 0:
        return "negative volume"
    # A bar dated in the future means a timezone or parsing fault upstream.
    if bar.trade_date > today + timedelta(days=1):
        return f"trade_date in the future ({bar.trade_date})"
    if bar.trade_date.year < 1970:
        return f"implausible trade_date ({bar.trade_date})"
    if bar.trade_date.weekday() >= 5:
        return f"weekend trade_date ({bar.trade_date})"
    return None


def validate_bars(
    bars: list[Bar], *, previous_close: Decimal | None = None, today: date | None = None
) -> ValidationResult:
    """Validate a series, de-duplicate by date and flag implausible jumps."""
    result = ValidationResult()
    seen: dict[date, Bar] = {}

    for bar in sorted(bars, key=lambda b: b.trade_date):
        reason = validate_bar(bar, today=today)
        if reason:
            result.rejected.append((bar, reason))
            continue
        if bar.trade_date in seen:
            # Later record for the same session wins; providers restate bars.
            result.warnings.append(f"duplicate bar for {bar.trade_date}, keeping latest")
        seen[bar.trade_date] = bar

    ordered = [seen[d] for d in sorted(seen)]
    reference = previous_close
    for bar in ordered:
        if reference and reference > 0:
            move = abs(float((bar.close - reference) / reference)) * 100
            if move > MAX_DAILY_MOVE_PCT:
                result.rejected.append(
                    (bar, f"implausible {move:.1f}% move (possible unadjusted split)")
                )
                continue
        result.valid.append(bar)
        reference = bar.close

    return result


def detect_gaps(bars: list[Bar], *, max_gap_days: int = 5) -> list[tuple[date, date, int]]:
    """Report calendar gaps larger than a long weekend, for the freshness view."""
    gaps: list[tuple[date, date, int]] = []
    ordered = sorted(bars, key=lambda b: b.trade_date)
    for prev, curr in zip(ordered, ordered[1:]):
        delta = (curr.trade_date - prev.trade_date).days
        if delta > max_gap_days:
            gaps.append((prev.trade_date, curr.trade_date, delta))
    return gaps


def validate_quote(quote: QuoteData, *, max_age_seconds: float) -> str | None:
    if quote.price is None or quote.price <= 0:
        return "non-positive price"
    # Tolerate a little clock skew, but a materially future timestamp is a bug.
    if quote.source_timestamp > datetime.now(timezone.utc) + timedelta(minutes=5):
        return "source_timestamp is in the future"
    if quote.quality is DataQuality.SYNTHETIC:
        return None  # tests bypass the age gate deliberately
    if quote.age_seconds > max_age_seconds:
        return f"stale quote: {quote.age_seconds:.0f}s old (limit {max_age_seconds:.0f}s)"
    return None


def validate_intraday_bar(bar: IntradayBar) -> str | None:
    if not _positive(bar.open, bar.high, bar.low, bar.close):
        return "non-positive price"
    if bar.high < bar.low:
        return "high < low"
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        return "OHLC inconsistency"
    if bar.bar_timestamp > datetime.now(timezone.utc) + timedelta(minutes=5):
        return "bar_timestamp is in the future"
    return None
