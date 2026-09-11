"""Zerodha Kite Connect adapter.

The binary tick protocol is the part most likely to be silently wrong: a
mis-decoded packet yields a plausible but incorrect price, which is worse than
an outright failure. Every packet layout, every segment divisor and every
malformed-frame path is therefore exercised against constructed bytes.
"""
from __future__ import annotations

import json
import struct
from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from app.core.exceptions import ProviderError, SymbolNotFound
from app.market_data.providers.kite import (
    API_ROOT, KiteAuthError, KiteConnectProvider, KiteSubscriptionError,
    _parse_ist, build_checksum, login_url,
)
from app.market_data.providers.kite_protocol import (
    DEFAULT_DIVISOR, TickDecodeError, decode_message, decode_packet,
    is_index, price_divisor, segment_of, split_packets,
)
from app.market_data.providers.kite_ticker import KiteTickerStream, _is_auth_failure
from app.market_data.streaming import StreamState
from app.models.enums import DataQuality

# NSE cash segment: the token's low byte is 1. 738561 is RELIANCE on NSE.
RELIANCE = 738561
NIFTY = 256265          # low byte 9 -> indices segment
USDINR = 0x100 | 3      # low byte 3 -> NSE currency segment


def i32(value: int) -> bytes:
    return struct.pack(">i", value)


def i16(value: int) -> bytes:
    return struct.pack(">h", value)


def quote_packet(token: int = RELIANCE, ltp: int = 140250) -> bytes:
    """A 44-byte quote-mode packet."""
    return (
        i32(token) + i32(ltp) + i32(50) + i32(139980) + i32(1234567)
        + i32(9000) + i32(8000) + i32(139000) + i32(141000) + i32(138500) + i32(139500)
    )


def full_packet(token: int = RELIANCE) -> bytes:
    """A 184-byte full-mode packet with ten depth entries."""
    packet = quote_packet(token)
    packet += i32(1757600000) + i32(4321) + i32(0) + i32(0) + i32(1757600100)
    for index in range(10):
        packet += i32(100 + index) + i32(140200 + index) + i16(5 + index) + i16(0)
    return packet


def index_packet(token: int = NIFTY, *, with_timestamp: bool = False) -> bytes:
    packet = (
        i32(token) + i32(2512340) + i32(2520000) + i32(2500000)
        + i32(2505000) + i32(2508000) + i32(4340)
    )
    return packet + i32(1757600100) if with_timestamp else packet


class TestSegmentsAndDivisors:
    def test_segment_is_the_low_byte(self):
        assert segment_of(RELIANCE) == 1
        assert segment_of(NIFTY) == 9

    def test_equity_prices_scale_by_one_hundred(self):
        assert price_divisor(RELIANCE) == DEFAULT_DIVISOR

    def test_currency_prices_scale_by_ten_million(self):
        assert price_divisor(USDINR) == 10_000_000.0

    def test_indices_are_recognised(self):
        assert is_index(NIFTY)
        assert not is_index(RELIANCE)


class TestPacketDecoding:
    def test_quote_packet_fields(self):
        tick = decode_packet(quote_packet())
        assert tick.instrument_token == RELIANCE
        assert tick.last_price == pytest.approx(1402.50)
        assert tick.ohlc_open == pytest.approx(1390.00)
        assert tick.ohlc_high == pytest.approx(1410.00)
        assert tick.ohlc_low == pytest.approx(1385.00)
        assert tick.ohlc_close == pytest.approx(1395.00)
        assert tick.volume_traded == 1_234_567
        assert tick.average_price == pytest.approx(1399.80)
        assert tick.mode == "quote"
        assert tick.tradable

    def test_quote_packet_carries_no_depth_or_timestamp(self):
        tick = decode_packet(quote_packet())
        assert tick.depth_buy == [] and tick.depth_sell == []
        assert tick.exchange_timestamp is None

    def test_change_percent_is_against_the_previous_close(self):
        tick = decode_packet(quote_packet())
        assert tick.change_pct == pytest.approx((1402.50 - 1395.00) / 1395.00 * 100)

    def test_full_packet_adds_depth_and_timestamps(self):
        tick = decode_packet(full_packet())
        assert tick.mode == "full"
        assert tick.open_interest == 4321
        assert tick.exchange_timestamp == datetime.fromtimestamp(1757600100, tz=timezone.utc)
        assert tick.last_trade_time == datetime.fromtimestamp(1757600000, tz=timezone.utc)
        assert len(tick.depth_buy) == 5
        assert len(tick.depth_sell) == 5

    def test_depth_splits_five_bids_then_five_offers(self):
        tick = decode_packet(full_packet())
        assert tick.depth_buy[0].price == pytest.approx(1402.00)
        assert tick.depth_buy[0].quantity == 100
        assert tick.depth_buy[0].orders == 5
        # The sixth entry is the first offer.
        assert tick.depth_sell[0].price == pytest.approx(1402.05)
        assert tick.depth_sell[0].quantity == 105

    def test_ltp_packet(self):
        tick = decode_packet(i32(RELIANCE) + i32(140250))
        assert tick.mode == "ltp"
        assert tick.last_price == pytest.approx(1402.50)
        assert tick.ohlc_open is None

    def test_index_packet_is_not_tradable(self):
        tick = decode_packet(index_packet())
        assert tick.last_price == pytest.approx(25123.40)
        assert tick.ohlc_close == pytest.approx(25080.00)
        assert tick.price_change == pytest.approx(43.40)
        assert not tick.tradable

    def test_index_full_packet_has_a_timestamp(self):
        tick = decode_packet(index_packet(with_timestamp=True))
        assert tick.mode == "full"
        assert tick.exchange_timestamp is not None

    def test_currency_packet_uses_its_own_divisor(self):
        tick = decode_packet(i32(USDINR) + i32(834_500_000))
        assert tick.last_price == pytest.approx(83.45)

    def test_zero_timestamp_means_absent_not_epoch(self):
        packet = quote_packet() + i32(0) * 5 + b"\x00" * 120
        tick = decode_packet(packet)
        assert tick.exchange_timestamp is None
        assert tick.last_trade_time is None

    def test_unknown_packet_length_is_refused(self):
        with pytest.raises(TickDecodeError, match="unrecognised packet length"):
            decode_packet(i32(RELIANCE) + i32(1) + b"\x00" * 5)

    def test_short_packet_is_refused(self):
        with pytest.raises(TickDecodeError):
            decode_packet(b"\x00\x01")


class TestFrameSplitting:
    def test_multiple_packets_in_one_frame(self):
        frame = (
            i16(3)
            + i16(44) + quote_packet()
            + i16(8) + i32(RELIANCE) + i32(140300)
            + i16(28) + index_packet()
        )
        ticks = decode_message(frame)
        assert len(ticks) == 3
        assert [t.mode for t in ticks] == ["quote", "ltp", "quote"]

    def test_heartbeat_yields_no_ticks(self):
        assert decode_message(i16(0)) == []
        assert decode_message(b"\x00") == []
        assert decode_message(b"") == []

    def test_truncated_frame_is_refused(self):
        with pytest.raises(TickDecodeError, match="truncated"):
            split_packets(i16(2) + i16(44) + quote_packet())

    def test_one_bad_packet_does_not_lose_the_others(self):
        frame = (
            i16(2)
            + i16(13) + (i32(RELIANCE) + i32(1) + b"\x00" * 5)   # unknown layout
            + i16(44) + quote_packet()
        )
        ticks = decode_message(frame)
        assert len(ticks) == 1
        assert ticks[0].last_price == pytest.approx(1402.50)


class TestTimestampHandling:
    def test_naive_timestamps_are_treated_as_ist(self):
        # 15:30 IST is 10:00 UTC. Reading it as UTC would misdate by 5h30m.
        parsed = _parse_ist("2026-09-11 15:30:00")
        assert parsed == datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

    def test_explicit_offsets_are_respected(self):
        assert _parse_ist("2026-09-11T15:30:00+05:30") == datetime(
            2026, 9, 11, 10, 0, tzinfo=timezone.utc
        )

    def test_unparseable_values_return_none(self):
        assert _parse_ist("not a timestamp") is None
        assert _parse_ist(None) is None
        assert _parse_ist("") is None


class TestAuthHelpers:
    def test_checksum_is_sha256_of_the_concatenation(self):
        import hashlib

        expected = hashlib.sha256(b"keyreqtoksecret").hexdigest()
        assert build_checksum("key", "reqtok", "secret") == expected

    def test_login_url_carries_the_api_key(self):
        assert "api_key=abc123" in login_url("abc123")

    def test_auth_failures_are_recognised(self):
        assert _is_auth_failure(Exception("server rejected WebSocket connection: HTTP 403"))
        assert _is_auth_failure(Exception("401 Unauthorized"))
        assert not _is_auth_failure(Exception("connection reset by peer"))


class TestRestAdapter:
    @pytest.fixture
    def provider(self):
        return KiteConnectProvider(api_key="testkey", access_token="testtoken")

    def test_symbol_mapping(self, provider):
        assert provider.to_kite_symbol("RELIANCE") == "NSE:RELIANCE"
        assert provider.to_kite_symbol("TCS.NS") == "NSE:TCS"
        assert provider.to_kite_symbol("TCS.BO") == "BSE:TCS"
        assert provider.to_kite_symbol("NSE:INFY") == "NSE:INFY"

    def test_unconfigured_provider_reports_itself(self):
        assert not KiteConnectProvider(api_key=None, access_token=None).is_configured()

    def test_quote_is_tagged_live(self, provider):
        payload = {
            "status": "success",
            "data": {
                "NSE:RELIANCE": {
                    "last_price": 1402.5,
                    "volume": 1234567,
                    "exchange_timestamp": "2026-09-11 15:30:00",
                    "ohlc": {"open": 1390, "high": 1410, "low": 1385, "close": 1395},
                }
            },
        }
        with respx.mock:
            respx.get(f"{API_ROOT}/quote").mock(return_value=httpx.Response(200, json=payload))
            quote = provider.fetch_quote("RELIANCE")

        assert float(quote.price) == pytest.approx(1402.5)
        # Kite is an exchange-licensed feed -- the only source here entitled
        # to claim LIVE.
        assert quote.quality is DataQuality.LIVE
        assert quote.source_timestamp == datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
        assert float(quote.previous_close) == pytest.approx(1395)

    def test_expired_token_raises_a_clear_auth_error(self, provider):
        payload = {
            "status": "error",
            "message": "Invalid `api_key` or `access_token`.",
            "error_type": "TokenException",
        }
        with respx.mock:
            respx.get(f"{API_ROOT}/quote").mock(return_value=httpx.Response(200, json=payload))
            with pytest.raises(KiteAuthError, match="expire each morning"):
                provider.fetch_quote("RELIANCE")

    def test_auth_errors_are_not_retried(self, provider):
        assert KiteAuthError("x").retryable is False

    def test_missing_subscription_is_named(self, provider):
        payload = {
            "status": "error",
            "message": "You do not have a subscription for historical data.",
            "error_type": "DataException",
        }
        with respx.mock:
            respx.get(url__startswith=f"{API_ROOT}/instruments/historical/").mock(
                return_value=httpx.Response(200, json=payload)
            )
            provider._instruments = {"NSE:RELIANCE": {"instrument_token": RELIANCE}}
            with pytest.raises(KiteSubscriptionError, match="paid Kite add-on"):
                provider.fetch_daily_bars("RELIANCE", date(2026, 1, 1), date(2026, 2, 1))

    def test_unknown_symbol_raises(self, provider):
        with respx.mock:
            respx.get(f"{API_ROOT}/quote").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": {}})
            )
            with pytest.raises(SymbolNotFound):
                provider.fetch_quote("NOSUCH")

    def test_instruments_csv_is_parsed(self, provider):
        csv_body = (
            "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
            "strike,tick_size,lot_size,instrument_type,segment,exchange\n"
            f"{RELIANCE},2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
            "2953217,11533,TCS,TATA CONSULTANCY,0,,0,0.05,1,EQ,NSE,NSE\n"
        )
        with respx.mock:
            respx.get(f"{API_ROOT}/instruments/NSE").mock(
                return_value=httpx.Response(200, text=csv_body)
            )
            table = provider.load_instruments("NSE", force=True)

        assert table["NSE:RELIANCE"]["instrument_token"] == RELIANCE
        assert provider.instrument_token("RELIANCE") == RELIANCE
        assert provider.instrument_token("TCS") == 2953217

    def test_historical_candles_become_bars(self, provider):
        payload = {
            "status": "success",
            "data": {
                "candles": [
                    ["2026-01-05T00:00:00+0530", 1390, 1410, 1385, 1402.5, 1234567],
                    ["2026-01-06T00:00:00+0530", 1402, 1420, 1398, 1415.0, 987654],
                ]
            },
        }
        provider._instruments = {"NSE:RELIANCE": {"instrument_token": RELIANCE}}
        provider._instruments_loaded_at = datetime.now(timezone.utc)
        with respx.mock:
            respx.get(url__startswith=f"{API_ROOT}/instruments/historical/").mock(
                return_value=httpx.Response(200, json=payload)
            )
            bars = provider.fetch_daily_bars("RELIANCE", date(2026, 1, 1), date(2026, 1, 31))

        assert len(bars) == 2
        assert float(bars[0].close) == pytest.approx(1402.5)
        # Historical candles are settled data, not a live price.
        assert bars[0].quality is DataQuality.EOD

    def test_session_exchange_sets_the_access_token(self):
        provider = KiteConnectProvider(api_key="testkey", access_token=None)
        payload = {
            "status": "success",
            "data": {"access_token": "fresh-token", "user_id": "AB1234",
                     "login_time": "2026-09-11 08:00:00"},
        }
        with respx.mock:
            respx.post(f"{API_ROOT}/session/token").mock(
                return_value=httpx.Response(200, json=payload)
            )
            result = provider.exchange_request_token("reqtok", "secret")

        assert result["access_token"] == "fresh-token"
        assert provider.access_token == "fresh-token"


class TestTickerStream:
    @pytest.fixture
    def stream(self):
        return KiteTickerStream(
            api_key="k", access_token="t",
            token_to_symbol={RELIANCE: "RELIANCE", NIFTY: "NIFTY50"},
        )

    def test_rejects_an_invalid_mode(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            KiteTickerStream(api_key="k", access_token="t", mode="bogus")

    def test_starts_disconnected(self, stream):
        assert stream.stats.state is StreamState.DISCONNECTED
        assert not stream.is_connected

    def test_refuses_to_start_unconfigured(self):
        unconfigured = KiteTickerStream(api_key=None, access_token=None)
        with pytest.raises(RuntimeError, match="KITE_API_KEY"):
            unconfigured.start()

    def test_tick_becomes_a_live_quote(self, stream):
        tick = decode_packet(quote_packet())
        quote = stream.tick_to_quote(tick)
        assert quote is not None
        assert quote.symbol == "RELIANCE"
        assert float(quote.price) == pytest.approx(1402.50)
        assert quote.quality is DataQuality.LIVE
        assert quote.provider == "kite-ticker"

    def test_an_unmapped_token_is_dropped_not_invented(self, stream):
        tick = decode_packet(quote_packet(token=999999 * 256 + 1))
        assert stream.tick_to_quote(tick) is None

    def test_full_mode_uses_the_exchange_timestamp(self, stream):
        quote = stream.tick_to_quote(decode_packet(full_packet()))
        assert quote.source_timestamp == datetime.fromtimestamp(1757600100, tz=timezone.utc)

    def test_quote_mode_falls_back_to_receipt_time(self, stream):
        received = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
        quote = stream.tick_to_quote(decode_packet(quote_packet()), received_at=received)
        # No exchange stamp exists in a 44-byte packet, so receipt time stands in.
        assert quote.source_timestamp == received

    def test_handlers_receive_ticks(self, stream):
        received = []
        stream.add_handler(received.append)
        stream._handle_binary(i16(1) + i16(44) + quote_packet())
        assert len(received) == 1
        assert received[0].symbol == "RELIANCE"
        assert stream.stats.ticks_received == 1

    def test_a_failing_handler_does_not_stop_the_others(self, stream):
        received = []

        def explode(_quote):
            raise RuntimeError("handler bug")

        stream.add_handler(explode)
        stream.add_handler(received.append)
        stream._handle_binary(i16(1) + i16(44) + quote_packet())
        assert len(received) == 1

    def test_subscription_respects_the_instrument_cap(self, stream, monkeypatch):
        from app.core.config import settings as live_settings

        monkeypatch.setattr(live_settings, "kite_stream_max_instruments", 2)
        stream.subscribe_tokens([1, 2, 3, 4, 5])
        assert len(stream.subscribed_tokens) == 2

    def test_subscribing_by_symbol_uses_the_mapping(self, stream):
        stream.subscribe(["RELIANCE", "NIFTY50", "UNKNOWN"])
        assert stream.subscribed_tokens == {RELIANCE, NIFTY}

    def test_unsubscribe_removes_tokens(self, stream):
        stream.subscribe(["RELIANCE", "NIFTY50"])
        stream.unsubscribe(["RELIANCE"])
        assert stream.subscribed_tokens == {NIFTY}

    def test_server_error_frames_are_recorded_not_raised(self, stream):
        stream._handle_text(json.dumps({"type": "error", "data": "subscription limit"}))
        assert "subscription limit" in stream.stats.last_error

    def test_order_updates_are_ignored(self, stream):
        # This adapter is market-data only; order frames must not be acted on.
        stream._handle_text(json.dumps({"type": "order", "data": {"order_id": "1"}}))
        assert stream.stats.last_error is None

    def test_health_reports_disconnection(self, stream):
        ok, detail = stream.health_check()
        assert not ok
        assert "disconnected" in detail.lower()

    def test_decode_errors_are_counted(self, stream):
        stream._handle_binary(i16(5) + i16(44) + quote_packet())   # claims 5, has 1
        assert stream.stats.decode_errors == 1
