"""Drives the Kite ticker against a local server speaking the real protocol.

The live endpoint (wss://ws.kite.trade) needs a paid subscription and is not
reachable from CI, so this stands a server in front of the adapter that emits
genuine Kite-format binary frames. It exercises the parts a unit test cannot:
the background thread, the asyncio connection loop, subscription handshakes,
resubscription after a drop, and clean shutdown.
"""
from __future__ import annotations

import asyncio
import json
import struct
import threading
import time

import pytest

from app.market_data.providers.kite_ticker import KiteTickerStream
from app.market_data.streaming import StreamState
from app.models.enums import DataQuality

pytestmark = [pytest.mark.integration]

RELIANCE = 738561
TCS = 2953217


def i32(v: int) -> bytes:
    return struct.pack(">i", v)


def i16(v: int) -> bytes:
    return struct.pack(">h", v)


def quote_packet(token: int, ltp_paise: int) -> bytes:
    return (
        i32(token) + i32(ltp_paise) + i32(50) + i32(ltp_paise) + i32(1_000_000)
        + i32(900) + i32(800) + i32(139000) + i32(141000) + i32(138500) + i32(139500)
    )


def frame(*packets: bytes) -> bytes:
    out = i16(len(packets))
    for packet in packets:
        out += i16(len(packet)) + packet
    return out


class FakeKiteServer:
    """A local websocket server that behaves like Kite's ticker endpoint."""

    def __init__(self):
        self.port: int | None = None
        self.control_messages: list[dict] = []
        self.connections = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._ready = threading.Event()
        self._clients: set = set()
        self.drop_next = False

    async def _handler(self, websocket):
        self.connections += 1
        self._clients.add(websocket)
        try:
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        self.control_messages.append(json.loads(message))
                    except ValueError:
                        pass
                    # Acknowledge a subscription with an immediate tick, as the
                    # real endpoint does.
                    await websocket.send(frame(quote_packet(RELIANCE, 140250)))
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)

    async def broadcast(self, payload: bytes):
        for client in list(self._clients):
            try:
                await client.send(payload)
            except Exception:
                pass

    def push(self, payload: bytes):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.broadcast(payload), self._loop)

    async def _close_all(self):
        for client in list(self._clients):
            try:
                await client.close(code=1001, reason="server going away")
            except Exception:
                pass

    def drop_all_clients(self):
        """Sever every open connection, as a real outage would."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(timeout=5)

    def _run(self):
        import websockets

        async def main():
            self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            await asyncio.Future()

        try:
            asyncio.run(main())
        except Exception:
            self._ready.set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=10), "fake Kite server failed to start"

    def stop(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def fake_server():
    server = FakeKiteServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def stream(fake_server, monkeypatch):
    """Point the adapter at the local server instead of wss://ws.kite.trade."""
    monkeypatch.setattr(
        "app.market_data.providers.kite_ticker.WS_URL",
        f"ws://127.0.0.1:{fake_server.port}",
    )
    ticker = KiteTickerStream(
        api_key="k", access_token="t",
        token_to_symbol={RELIANCE: "RELIANCE", TCS: "TCS"},
    )
    yield ticker
    ticker.stop()


def _wait_for(predicate, timeout=10.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestStreamLifecycle:
    def test_connects_and_reports_connected(self, stream):
        stream.start()
        assert _wait_for(lambda: stream.is_connected), stream.stats.last_error
        assert stream.stats.state is StreamState.CONNECTED
        assert stream.stats.connected_at is not None

    def test_subscription_handshake_is_sent(self, stream, fake_server):
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.subscribe(["RELIANCE", "TCS"])

        assert _wait_for(lambda: len(fake_server.control_messages) >= 2)
        actions = [m.get("a") for m in fake_server.control_messages]
        assert "subscribe" in actions
        assert "mode" in actions

        subscribe = next(m for m in fake_server.control_messages if m["a"] == "subscribe")
        assert sorted(subscribe["v"]) == sorted([RELIANCE, TCS])

        mode = next(m for m in fake_server.control_messages if m["a"] == "mode")
        assert mode["v"][0] == "quote"

    def test_ticks_reach_handlers_as_live_quotes(self, stream, fake_server):
        received = []
        stream.add_handler(received.append)
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.subscribe(["RELIANCE"])

        assert _wait_for(lambda: len(received) >= 1, timeout=10), "no tick arrived"
        quote = received[0]
        assert quote.symbol == "RELIANCE"
        assert float(quote.price) == pytest.approx(1402.50)
        assert quote.quality is DataQuality.LIVE

    def test_multiple_instruments_in_one_frame(self, stream, fake_server):
        received = []
        stream.add_handler(received.append)
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.subscribe(["RELIANCE", "TCS"])
        assert _wait_for(lambda: stream.stats.messages_received >= 1)

        fake_server.push(
            frame(quote_packet(RELIANCE, 141000), quote_packet(TCS, 350000))
        )
        assert _wait_for(lambda: len(received) >= 3, timeout=10)
        symbols = {q.symbol for q in received}
        assert {"RELIANCE", "TCS"} <= symbols

    def test_heartbeats_do_not_produce_ticks(self, stream):
        received = []
        stream.add_handler(received.append)
        stream.start()
        assert _wait_for(lambda: stream.is_connected)

        before = len(received)
        for _ in range(5):
            # A zero-packet frame is Kite's heartbeat.
            pytest.importorskip("websockets")
        stream._handle_binary(i16(0))
        assert len(received) == before

    def test_reconnects_and_resubscribes_after_a_drop(self, stream, fake_server):
        received = []
        stream.add_handler(received.append)
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.subscribe(["RELIANCE"])
        assert _wait_for(lambda: len(received) >= 1)

        connections_before = fake_server.connections
        fake_server.drop_all_clients()

        # The adapter must reconnect on its own and restore its subscriptions;
        # Kite keeps no server-side memory of them.
        assert _wait_for(
            lambda: fake_server.connections > connections_before, timeout=20
        ), "did not reconnect"
        assert _wait_for(lambda: stream.is_connected, timeout=20)
        assert stream.stats.reconnects >= 1
        assert stream.subscribed_tokens == {RELIANCE}

    def test_stop_is_clean(self, stream):
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.stop()
        assert stream.stats.state is StreamState.STOPPED
        assert not stream.is_connected

    def test_stats_are_reported(self, stream):
        stream.start()
        assert _wait_for(lambda: stream.is_connected)
        stream.subscribe(["RELIANCE"])
        assert _wait_for(lambda: stream.stats.ticks_received >= 1)

        status = stream.stats.as_dict()
        assert status["state"] == "CONNECTED"
        assert status["ticks_received"] >= 1
        assert status["subscribed_count"] == 1
        assert status["seconds_since_tick"] is not None
