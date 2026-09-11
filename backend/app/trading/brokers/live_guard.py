"""Guards that stand between a signal and real money.

Live trading requires THREE independent conditions, all explicit:
  1. TRADING_MODE=live
  2. LIVE_TRADING_ENABLED=true
  3. a configured, connected live broker adapter

Any one of them missing keeps the platform in paper mode. The kill switch
overrides all three.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.core.exceptions import KillSwitchEngaged
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LiveTradingStatus:
    allowed: bool
    mode: str
    blockers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "mode": self.mode, "blockers": list(self.blockers)}


def live_trading_status(*, broker_connected: bool | None = None) -> LiveTradingStatus:
    blockers: list[str] = []

    if settings.kill_switch_engaged:
        blockers.append("kill switch is engaged")
    if settings.trading_mode != "live":
        blockers.append(f"TRADING_MODE is '{settings.trading_mode}', not 'live'")
    if not settings.live_trading_enabled:
        blockers.append("LIVE_TRADING_ENABLED is false")
    if broker_connected is False:
        blockers.append("no live broker adapter is connected")

    return LiveTradingStatus(
        allowed=not blockers,
        mode=settings.trading_mode,
        blockers=tuple(blockers),
    )


def assert_trading_allowed() -> None:
    """Raise when the kill switch is engaged. Called before every order."""
    if settings.kill_switch_engaged:
        raise KillSwitchEngaged(
            "trading is halted by the kill switch; no orders will be placed"
        )
