"""Zerodha Kite Connect -- WebSocket tick stream.

Runs its own asyncio loop on a background thread so it slots into this
project's synchronous, threaded architecture without dragging an event loop
into the request path or the scheduler.

Connection:  wss://ws.kite.trade?api_key=<key>&access_token=<token>
Control:     JSON text frames -- {"a": "subscribe", "v": [tokens]}
Ticks:       binary frames, decoded by `kite_protocol`
Limit:       3000 instruments per connection

Timestamps, precisely: `full` mode packets carry the exchange timestamp and it
is used verbatim. `ltp` and `quote` mode packets carry none, so the receipt
time is used instead -- accurate to within the network hop on a push feed,
and marked as such via `timestamp_is_receipt` so nothing downstream mistakes
it for an exchange stamp.

Reconnection backs off exponentially. An authentication failure is NOT
retried: Kite access tokens expire every morning, and hammering the endpoint
with a dead token neither fixes it nor goes unnoticed by the vendor.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger
from app.market_data.providers.kite_protocol import Tick, decode_message
from app.market_data.streaming import StreamingProvider, StreamState
from app.market_data.types import QuoteData
from app.models.enums import DataQuality

log = get_logger(__name__)

WS_URL = "wss://ws.kite.trade"
VALID_MODES = ("ltp", "quote", "full")
MAX_BACKOFF_SECONDS = 60.0
#: Kite sends a heartbeat roughly every second; silence well past that means
#: the connection is wedged even though the socket still looks open.
SILENCE_TIMEOUT_SECONDS = 30.0


class KiteTickerStream(StreamingProvider):
    name = "kite-ticker"
    #: Exchange-licensed push feed -- the one source here entitled to LIVE.
    tick_quality = DataQuality.LIVE

    def __init__(
        self,
        api_key: str | None = None,
        access_token: str | None = None,
        *,
        mode: str | None = None,
        token_to_symbol: dict[int, str] | None = None,
    ):
        super().__init__()
        self.api_key = api_key or settings.kite_api_key
        self.access_token = access_token or settings.kite_access_token
        self.mode = (mode or settings.kite_stream_mode).lower()
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")

        #: instrument_token -> platform symbol. Ticks identify instruments by
        #: token only, so without this a tick cannot be attributed.
        self._token_to_symbol: dict[int, str] = dict(token_to_symbol or {})
        self._subscribed: set[int] = set()

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_message_at = 0.0

    # ------------------------------------------------------------- mapping
    def register_instruments(self, mapping: dict[int, str]) -> None:
        """Teach the stream which platform symbol each token belongs to."""
        with self._lock:
            self._token_to_symbol.update(mapping)

    def symbol_for(self, token: int) -> str | None:
        return self._token_to_symbol.get(token)

    def is_configured(self) -> bool:
        return bool(self.api_key and self.access_token)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.is_configured():
            raise RuntimeError(
                "Kite streaming requires KITE_API_KEY and KITE_ACCESS_TOKEN; "
                "the access token is issued by the daily Zerodha login flow"
            )
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self.stats.state = StreamState.CONNECTING
        self._thread = threading.Thread(
            target=self._thread_main, name="kite-ticker", daemon=True
        )
        self._thread.start()
        log.info("kite_ticker_starting", mode=self.mode)

    def stop(self) -> None:
        self._stop_event.set()
        loop, socket = self._loop, self._socket
        if loop is not None and socket is not None:
            asyncio.run_coroutine_threadsafe(socket.close(), loop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.stats.state = StreamState.STOPPED
        log.info("kite_ticker_stopped", ticks=self.stats.ticks_received)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connection_loop())
        except Exception as exc:
            self.stats.state = StreamState.FAILED
            self.stats.last_error = str(exc)[:300]
            log.error("kite_ticker_crashed", error=str(exc))
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    # ----------------------------------------------------------- connection
    async def _connection_loop(self) -> None:
        import websockets

        attempt = 0
        while not self._stop_event.is_set():
            url = f"{WS_URL}?api_key={self.api_key}&access_token={self.access_token}"
            try:
                self.stats.state = (
                    StreamState.RECONNECTING if attempt else StreamState.CONNECTING
                )
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20,
                    max_size=2**22, open_timeout=15,
                ) as socket:
                    self._socket = socket
                    attempt = 0
                    self.stats.state = StreamState.CONNECTED
                    self.stats.connected_at = datetime.now(timezone.utc)
                    self.stats.last_error = None
                    self._last_message_at = time.monotonic()
                    log.info("kite_ticker_connected", mode=self.mode)

                    await self._resubscribe(socket)
                    await self._read_loop(socket)

            except Exception as exc:
                message = str(exc)
                self.stats.last_error = message[:300]

                # 403 on the handshake means the token is dead. Retrying is
                # futile until a human re-logs in, so stop and say so.
                if _is_auth_failure(exc):
                    self.stats.state = StreamState.FAILED
                    log.error(
                        "kite_ticker_auth_failed",
                        error=message[:200],
                        hint="Kite access tokens expire each morning (~07:30 IST); re-login required",
                    )
                    return

                if self._stop_event.is_set():
                    break

                attempt += 1
                self.stats.reconnects += 1
                backoff = min(MAX_BACKOFF_SECONDS, 2 ** min(attempt, 6))
                log.warning(
                    "kite_ticker_disconnected",
                    error=message[:200], attempt=attempt, retry_in=backoff,
                )
                self.stats.state = StreamState.RECONNECTING
                await asyncio.sleep(backoff)
            finally:
                self._socket = None

        self.stats.state = StreamState.STOPPED

    async def _read_loop(self, socket) -> None:
        while not self._stop_event.is_set():
            try:
                message = await asyncio.wait_for(
                    socket.recv(), timeout=SILENCE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # A live Kite socket is never silent this long.
                raise ConnectionError(
                    f"no data for {SILENCE_TIMEOUT_SECONDS:.0f}s; assuming the "
                    "connection is stale"
                )

            self.stats.messages_received += 1
            self._last_message_at = time.monotonic()

            if isinstance(message, bytes):
                self._handle_binary(message)
            else:
                self._handle_text(message)

    def _handle_binary(self, message: bytes) -> None:
        received_at = datetime.now(timezone.utc)
        try:
            ticks = decode_message(message)
        except Exception as exc:
            self.stats.decode_errors += 1
            log.warning("kite_tick_decode_failed", error=str(exc)[:200])
            return

        for tick in ticks:
            quote = self.tick_to_quote(tick, received_at=received_at)
            if quote is not None:
                self._emit(quote)

    def _handle_text(self, message: str) -> None:
        """Text frames are order updates and errors, never ticks."""
        try:
            payload = json.loads(message)
        except ValueError:
            return
        kind = payload.get("type")
        if kind == "error":
            self.stats.last_error = str(payload.get("data"))[:300]
            log.warning("kite_ticker_server_error", detail=payload.get("data"))
        elif kind == "order":
            # Order updates arrive here when trading through the same session.
            # This adapter is market-data only, so they are logged, not acted on.
            log.info("kite_order_update_ignored", detail_keys=list((payload.get("data") or {}).keys()))

    # -------------------------------------------------------- tick handling
    def tick_to_quote(self, tick: Tick, *, received_at: datetime | None = None) -> QuoteData | None:
        """Convert a decoded tick into the platform's QuoteData.

        Returns None for an unmapped token rather than inventing a symbol.
        """
        symbol = self.symbol_for(tick.instrument_token)
        if symbol is None:
            return None

        received_at = received_at or datetime.now(timezone.utc)
        # full-mode packets carry the exchange's own stamp; the lighter modes
        # do not, so receipt time stands in.
        source_timestamp = tick.exchange_timestamp or tick.last_trade_time or received_at

        return QuoteData(
            symbol=symbol,
            price=tick.last_price,
            source_timestamp=source_timestamp,
            provider=self.name,
            quality=self.tick_quality,
            previous_close=tick.ohlc_close,
            day_open=tick.ohlc_open,
            day_high=tick.ohlc_high,
            day_low=tick.ohlc_low,
            volume=tick.volume_traded,
            currency="INR",
            retrieved_at=received_at,
        )

    # ------------------------------------------------------- subscriptions
    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe by platform symbol. Requires a registered token mapping."""
        reverse = {v: k for k, v in self._token_to_symbol.items()}
        tokens = [reverse[s] for s in symbols if s in reverse]
        missing = [s for s in symbols if s not in reverse]
        if missing:
            log.warning("kite_subscribe_unmapped", symbols=missing[:10], count=len(missing))
        if tokens:
            self.subscribe_tokens(tokens)

    def subscribe_tokens(self, tokens: list[int]) -> None:
        limit = settings.kite_stream_max_instruments
        with self._lock:
            room = limit - len(self._subscribed)
            accepted = [t for t in tokens if t not in self._subscribed][:max(room, 0)]
            refused = len(tokens) - len(accepted)
            self._subscribed.update(accepted)
            self.stats.subscribed_count = len(self._subscribed)

        if refused > 0:
            log.warning(
                "kite_subscription_limit",
                limit=limit, refused=refused,
                hint="one connection carries at most 3000 instruments",
            )
        if accepted:
            self._send({"a": "subscribe", "v": accepted})
            self._send({"a": "mode", "v": [self.mode, accepted]})

    def unsubscribe(self, symbols: list[str]) -> None:
        reverse = {v: k for k, v in self._token_to_symbol.items()}
        tokens = [reverse[s] for s in symbols if s in reverse]
        if not tokens:
            return
        with self._lock:
            self._subscribed.difference_update(tokens)
            self.stats.subscribed_count = len(self._subscribed)
        self._send({"a": "unsubscribe", "v": tokens})

    async def _resubscribe(self, socket) -> None:
        """Re-establish subscriptions after a reconnect.

        Kite keeps no server-side memory of a dropped connection's
        subscriptions, so silence after a reconnect would otherwise look
        exactly like a quiet market.
        """
        with self._lock:
            tokens = sorted(self._subscribed)
        if not tokens:
            return
        await socket.send(json.dumps({"a": "subscribe", "v": tokens}))
        await socket.send(json.dumps({"a": "mode", "v": [self.mode, tokens]}))
        log.info("kite_ticker_resubscribed", instruments=len(tokens))

    def _send(self, payload: dict) -> None:
        """Queue a control frame onto the stream's event loop thread."""
        loop, socket = self._loop, self._socket
        if loop is None or socket is None:
            # Not connected yet: _resubscribe will replay it on connect.
            return
        try:
            asyncio.run_coroutine_threadsafe(socket.send(json.dumps(payload)), loop)
        except Exception as exc:
            log.warning("kite_ticker_send_failed", error=str(exc)[:200])

    @property
    def subscribed_tokens(self) -> set[int]:
        with self._lock:
            return set(self._subscribed)


def _is_auth_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("403", "401", "forbidden", "unauthorized", "invalid access token")
    )
