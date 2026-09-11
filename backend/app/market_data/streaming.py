"""Streaming market-data contract.

The `MarketDataProvider` interface is request/response: callers ask, adapters
answer. A streaming feed inverts that -- the venue pushes ticks and the
application reacts -- so it needs its own contract rather than being bent into
the polling one.

A streaming provider is the only kind that can legitimately produce
`DataQuality.LIVE`, and only when it is an exchange-licensed feed. Delayed
feeds must keep tagging their output DELAYED however they are transported:
pushing a stale price over a socket does not make it current.
"""
from __future__ import annotations

import abc
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.core.logging import get_logger
from app.market_data.types import QuoteData

log = get_logger(__name__)

#: Called with each tick. Must not block -- it runs on the reader thread.
TickHandler = Callable[[QuoteData], None]


class StreamState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class StreamStats:
    """Observability for a feed that is supposed to run unattended for hours."""

    state: StreamState = StreamState.DISCONNECTED
    connected_at: datetime | None = None
    last_tick_at: datetime | None = None
    ticks_received: int = 0
    messages_received: int = 0
    reconnects: int = 0
    subscribed_count: int = 0
    last_error: str | None = None
    decode_errors: int = 0

    @property
    def seconds_since_tick(self) -> float | None:
        if self.last_tick_at is None:
            return None
        return (datetime.now(timezone.utc) - self.last_tick_at).total_seconds()

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "seconds_since_tick": (
                round(self.seconds_since_tick, 1)
                if self.seconds_since_tick is not None else None
            ),
            "ticks_received": self.ticks_received,
            "messages_received": self.messages_received,
            "reconnects": self.reconnects,
            "subscribed_count": self.subscribed_count,
            "decode_errors": self.decode_errors,
            "last_error": self.last_error,
        }


class StreamingProvider(abc.ABC):
    """A push feed.

    Implementations run their own connection loop on a background thread and
    invoke registered handlers per tick. `start` returns immediately.
    """

    name: str = "base-stream"
    #: Freshness of ticks from this feed. Only an exchange-licensed feed may
    #: declare LIVE.
    tick_quality: str = "DELAYED"

    def __init__(self) -> None:
        self._handlers: list[TickHandler] = []
        self._handler_lock = threading.Lock()
        self.stats = StreamStats()

    # ------------------------------------------------------------- handlers
    def add_handler(self, handler: TickHandler) -> None:
        with self._handler_lock:
            self._handlers.append(handler)

    def _emit(self, quote: QuoteData) -> None:
        """Fan a tick out to handlers.

        A failing handler is logged and skipped: one bad consumer must not take
        down the feed for every other consumer.
        """
        self.stats.ticks_received += 1
        self.stats.last_tick_at = datetime.now(timezone.utc)
        with self._handler_lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(quote)
            except Exception as exc:
                log.error(
                    "tick_handler_failed", provider=self.name,
                    symbol=quote.symbol, error=str(exc),
                )

    # ------------------------------------------------------------ lifecycle
    @abc.abstractmethod
    def is_configured(self) -> bool:
        ...

    @abc.abstractmethod
    def start(self) -> None:
        """Connect and begin streaming. Returns immediately."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Disconnect and stop reconnecting."""

    @abc.abstractmethod
    def subscribe(self, symbols: list[str]) -> None:
        ...

    @abc.abstractmethod
    def unsubscribe(self, symbols: list[str]) -> None:
        ...

    @property
    def is_connected(self) -> bool:
        return self.stats.state is StreamState.CONNECTED

    def health_check(self) -> tuple[bool, str]:
        """Silence on a connected socket is still a fault worth reporting."""
        if not self.is_configured():
            return False, "not configured"
        if self.stats.state is StreamState.STOPPED:
            return False, "stopped"
        if self.stats.state is not StreamState.CONNECTED:
            return False, f"{self.stats.state.value.lower()}: {self.stats.last_error or 'no detail'}"
        idle = self.stats.seconds_since_tick
        if idle is not None and idle > 300:
            return False, f"connected but no tick for {idle:.0f}s"
        return True, (
            f"connected, {self.stats.ticks_received:,} ticks, "
            f"{self.stats.subscribed_count} instruments"
        )
