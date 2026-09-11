"""Kite Connect v3 binary tick protocol.

Isolated from transport so it can be tested exhaustively against constructed
packets. Getting this wrong produces plausible-looking but wrong prices, which
is worse than an outright failure, so every field is decoded explicitly and
every packet length is validated rather than assumed.

Wire format (all integers big-endian, signed):

    message := int16 packet_count, then per packet: int16 length, bytes payload

A message with packet_count 0 is a heartbeat. Text frames are JSON (order
updates and errors), not ticks.

Packet layouts by length:

     8  LTP          token, ltp
    28  index quote  token, ltp, high, low, open, close, price_change
    32  index full   ...as above, plus exchange_timestamp
    44  quote        token, ltp, last_qty, avg_price, volume, buy_qty,
                     sell_qty, open, high, low, close
   184  full         ...as quote, plus last_trade_time, oi, oi_high, oi_low,
                     exchange_timestamp, and 10 x 12-byte depth entries

Prices arrive as integers scaled by the instrument's segment divisor.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Packet sizes that carry a full equity/derivative quote.
LTP_PACKET = 8
INDEX_QUOTE_PACKET = 28
INDEX_FULL_PACKET = 32
QUOTE_PACKET = 44
FULL_PACKET = 184

DEPTH_ENTRY_BYTES = 12
DEPTH_ENTRIES = 10          # 5 bid + 5 ask
DEPTH_OFFSET = 64

# Segment is the low byte of the instrument token. Price divisors differ per
# segment: currency quotes to seven decimal places, BCD to four, everything
# else to two.
SEGMENT_NSE_CM = 1
SEGMENT_NSE_FO = 2
SEGMENT_NSE_CD = 3
SEGMENT_BSE_CM = 4
SEGMENT_BSE_FO = 5
SEGMENT_BSE_CD = 6
SEGMENT_MCX_FO = 7
SEGMENT_MCX_SX = 8
SEGMENT_INDICES = 9

_DIVISORS = {SEGMENT_NSE_CD: 10_000_000.0, SEGMENT_BSE_CD: 10_000.0}
DEFAULT_DIVISOR = 100.0


def segment_of(instrument_token: int) -> int:
    return instrument_token & 0xFF


def price_divisor(instrument_token: int) -> float:
    return _DIVISORS.get(segment_of(instrument_token), DEFAULT_DIVISOR)


def is_index(instrument_token: int) -> bool:
    return segment_of(instrument_token) == SEGMENT_INDICES


@dataclass(slots=True)
class DepthLevel:
    quantity: int
    price: float
    orders: int


@dataclass(slots=True)
class Tick:
    """One decoded tick. Fields absent from the packet's mode stay None."""

    instrument_token: int
    last_price: float
    mode: str
    tradable: bool
    last_quantity: int | None = None
    average_price: float | None = None
    volume_traded: int | None = None
    total_buy_quantity: int | None = None
    total_sell_quantity: int | None = None
    ohlc_open: float | None = None
    ohlc_high: float | None = None
    ohlc_low: float | None = None
    ohlc_close: float | None = None
    price_change: float | None = None
    open_interest: int | None = None
    last_trade_time: datetime | None = None
    exchange_timestamp: datetime | None = None
    depth_buy: list[DepthLevel] = field(default_factory=list)
    depth_sell: list[DepthLevel] = field(default_factory=list)

    @property
    def change_pct(self) -> float | None:
        if not self.ohlc_close:
            return None
        return (self.last_price - self.ohlc_close) / self.ohlc_close * 100


class TickDecodeError(ValueError):
    """A packet could not be decoded. Never guessed at."""


def _i32(buffer: bytes, offset: int) -> int:
    return struct.unpack_from(">i", buffer, offset)[0]


def _i16(buffer: bytes, offset: int) -> int:
    return struct.unpack_from(">h", buffer, offset)[0]


def _epoch(value: int) -> datetime | None:
    """Kite sends 0 when a timestamp is unset; that is absent, not 1970."""
    if not value or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def split_packets(message: bytes) -> list[bytes]:
    """Split a binary frame into its packets.

    Returns an empty list for a heartbeat. Raises on a truncated frame rather
    than returning what happened to fit.
    """
    if len(message) < 2:
        # Kite's heartbeat is a 1-byte frame.
        return []
    count = _i16(message, 0)
    if count <= 0:
        return []

    packets: list[bytes] = []
    offset = 2
    for index in range(count):
        if offset + 2 > len(message):
            raise TickDecodeError(
                f"truncated frame: packet {index} length header runs past the end"
            )
        length = _i16(message, offset)
        offset += 2
        if length < 0 or offset + length > len(message):
            raise TickDecodeError(
                f"truncated frame: packet {index} claims {length} bytes, "
                f"{len(message) - offset} remain"
            )
        packets.append(message[offset : offset + length])
        offset += length
    return packets


def decode_packet(packet: bytes) -> Tick:
    """Decode one packet into a Tick. Raises on an unrecognised length."""
    size = len(packet)
    if size < LTP_PACKET:
        raise TickDecodeError(f"packet too short: {size} bytes")

    token = _i32(packet, 0)
    divisor = price_divisor(token)
    last_price = _i32(packet, 4) / divisor

    # ------------------------------------------------------------- indices
    if size in (INDEX_QUOTE_PACKET, INDEX_FULL_PACKET):
        tick = Tick(
            instrument_token=token,
            last_price=last_price,
            mode="full" if size == INDEX_FULL_PACKET else "quote",
            tradable=False,          # an index cannot be traded directly
            ohlc_high=_i32(packet, 8) / divisor,
            ohlc_low=_i32(packet, 12) / divisor,
            ohlc_open=_i32(packet, 16) / divisor,
            ohlc_close=_i32(packet, 20) / divisor,
            price_change=_i32(packet, 24) / divisor,
        )
        if size == INDEX_FULL_PACKET:
            tick.exchange_timestamp = _epoch(_i32(packet, 28))
        return tick

    # ----------------------------------------------------------- ltp mode
    if size == LTP_PACKET:
        return Tick(
            instrument_token=token, last_price=last_price,
            mode="ltp", tradable=True,
        )

    # ------------------------------------------------- quote and full mode
    if size not in (QUOTE_PACKET, FULL_PACKET):
        raise TickDecodeError(
            f"unrecognised packet length {size}; refusing to guess its layout"
        )

    tick = Tick(
        instrument_token=token,
        last_price=last_price,
        mode="full" if size == FULL_PACKET else "quote",
        tradable=True,
        last_quantity=_i32(packet, 8),
        average_price=_i32(packet, 12) / divisor,
        volume_traded=_i32(packet, 16),
        total_buy_quantity=_i32(packet, 20),
        total_sell_quantity=_i32(packet, 24),
        ohlc_open=_i32(packet, 28) / divisor,
        ohlc_high=_i32(packet, 32) / divisor,
        ohlc_low=_i32(packet, 36) / divisor,
        ohlc_close=_i32(packet, 40) / divisor,
    )

    if size == FULL_PACKET:
        tick.last_trade_time = _epoch(_i32(packet, 44))
        tick.open_interest = _i32(packet, 48)
        tick.exchange_timestamp = _epoch(_i32(packet, 60))

        for index in range(DEPTH_ENTRIES):
            offset = DEPTH_OFFSET + index * DEPTH_ENTRY_BYTES
            level = DepthLevel(
                quantity=_i32(packet, offset),
                price=_i32(packet, offset + 4) / divisor,
                orders=_i16(packet, offset + 8),
            )
            # First five entries are bids, the next five are offers.
            (tick.depth_buy if index < 5 else tick.depth_sell).append(level)

    return tick


def decode_message(message: bytes) -> list[Tick]:
    """Decode a binary frame into ticks.

    A packet that cannot be decoded is skipped rather than discarding the whole
    frame -- one malformed instrument should not cost every other tick in the
    same message.
    """
    ticks: list[Tick] = []
    for packet in split_packets(message):
        try:
            ticks.append(decode_packet(packet))
        except TickDecodeError:
            continue
    return ticks
