"""Exchange calendars and session awareness.

Holiday lists are explicit data, not guesses. An exchange with no holiday list
for a given year falls back to weekday-only logic and says so via
`holidays_known`, so callers can distinguish "open" from "we don't know".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Published exchange holidays. Extend per year; absence is reported, not faked.
EXCHANGE_HOLIDAYS: dict[str, dict[int, set[str]]] = {
    "NSE": {
        2026: {
            "2026-01-26", "2026-03-04", "2026-03-25", "2026-04-01", "2026-04-03",
            "2026-04-14", "2026-05-01", "2026-08-15", "2026-09-14", "2026-10-02",
            "2026-10-21", "2026-11-09", "2026-12-25",
        }
    },
    "NYSE": {
        2026: {
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
            "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        }
    },
}
EXCHANGE_HOLIDAYS["BSE"] = EXCHANGE_HOLIDAYS["NSE"]
EXCHANGE_HOLIDAYS["NASDAQ"] = EXCHANGE_HOLIDAYS["NYSE"]


@dataclass(frozen=True, slots=True)
class SessionInfo:
    exchange: str
    is_open: bool
    local_time: datetime
    next_open: datetime | None
    next_close: datetime | None
    reason: str
    holidays_known: bool


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def is_holiday(exchange_code: str, day: date) -> tuple[bool, bool]:
    """Return (is_holiday, year_is_known)."""
    table = EXCHANGE_HOLIDAYS.get(exchange_code.upper())
    if not table or day.year not in table:
        return False, False
    return day.isoformat() in table[day.year], True


def get_session_info(
    exchange_code: str,
    tz_name: str,
    open_time: str,
    close_time: str,
    *,
    now: datetime | None = None,
) -> SessionInfo:
    tz = ZoneInfo(tz_name)
    local = (now or datetime.now(tz)).astimezone(tz)
    o, c = _parse_hhmm(open_time), _parse_hhmm(close_time)

    holiday, known = is_holiday(exchange_code, local.date())
    weekend = local.weekday() >= 5

    if weekend:
        reason, is_open = "weekend", False
    elif holiday:
        reason, is_open = "exchange holiday", False
    else:
        is_open = o <= local.time() < c
        reason = "regular session" if is_open else "outside session hours"

    return SessionInfo(
        exchange=exchange_code,
        is_open=is_open,
        local_time=local,
        next_open=_next_open(local, tz, o, exchange_code) if not is_open else None,
        next_close=local.replace(hour=c.hour, minute=c.minute, second=0, microsecond=0)
        if is_open
        else None,
        reason=reason,
        holidays_known=known,
    )


def _next_open(local: datetime, tz: ZoneInfo, o: time, exchange_code: str) -> datetime | None:
    candidate = local.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    # Look ahead at most a fortnight; beyond that the holiday table is unreliable.
    for _ in range(14):
        holiday, _known = is_holiday(exchange_code, candidate.date())
        if candidate.weekday() < 5 and not holiday:
            return candidate
        candidate += timedelta(days=1)
    return None


def previous_trading_day(exchange_code: str, day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(14):
        holiday, _ = is_holiday(exchange_code, cursor)
        if cursor.weekday() < 5 and not holiday:
            return cursor
        cursor -= timedelta(days=1)
    return cursor
