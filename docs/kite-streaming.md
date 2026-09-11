# Real-time data via Zerodha Kite Connect

This is the only path in the platform to genuinely real-time Indian market
data. Everything else — Yahoo, Stooq, Alpha Vantage, Finnhub — is delayed or
end-of-day, and is tagged as such.

## What changes when you enable it

| | Default (Yahoo) | Kite streaming |
|---|---|---|
| Freshness tag | `DELAYED` | **`LIVE`** |
| Transport | HTTP poll every 15 min | WebSocket push |
| End-to-end lag | up to ~30 min | sub-second |
| Coverage | NSE, BSE, NYSE, NASDAQ | **NSE, BSE only** |
| Cost | free | ~₹2,000/month |

Kite covers no US listings. The provider chain falls through to Yahoo for
those, so a mixed universe keeps working — Indian symbols go live, US symbols
stay delayed, and each is labelled honestly.

## Cost

- **Kite Connect API** — ~₹2,000/month
- **Historical candles** — a further ~₹2,000/month add-on. Without it,
  `fetch_daily_bars` raises a clear `KiteSubscriptionError` rather than
  returning nothing and letting you guess why.
- Requires an active Zerodha trading account.

## Setup

**1. Create an app** at <https://developers.kite.trade/> and note the API key
and secret.

**2. Configure:**

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_STREAMING_ENABLED=true
KITE_STREAM_MODE=quote
MARKET_DATA_PROVIDERS=kite,yahoo,stooq   # kite first for Indian symbols
```

**3. Mint today's access token.** Zerodha expires tokens every morning around
07:30 IST — that is their policy, not a limitation of this code:

```bash
# as an admin user
curl -H "Authorization: Bearer $TOKEN" \
  localhost:8000/api/v1/system/kite/login-url
# open the returned URL, sign in, copy request_token from the redirect
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/system/kite/session?request_token=XXXX"
```

Put the returned `access_token` in `KITE_ACCESS_TOKEN` and restart the worker.

**The server does not persist the access token.** Storing a live broker
credential in the application database is a decision for you to make
deliberately, not a default this code makes on your behalf.

**4. Verify:**

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/system/stream
```

```json
{
  "enabled": true, "configured": true, "started": true,
  "state": "CONNECTED", "mode": "quote",
  "ticks_received": 14823, "subscribed_count": 23,
  "seconds_since_tick": 0.3, "reconnects": 0
}
```

## Stream modes

| Mode | Packet | Carries |
|---|---|---|
| `ltp` | 8 bytes | last price only |
| `quote` | 44 bytes | price, OHLC, volume, buy/sell quantity — **default** |
| `full` | 184 bytes | adds 5-level market depth and exchange timestamps |

`quote` is the sensible default: it carries everything the signal engine uses
without the depth payload, which is four times the bandwidth for information
nothing downstream consumes.

One nuance worth knowing: only `full` packets carry an exchange timestamp.
In `ltp` and `quote` mode the adapter uses receipt time, which on a push feed
is accurate to the network hop — but it is receipt time, and the code says so
rather than pretending otherwise.

## Architecture

```
  wss://ws.kite.trade
          │  binary frames
          ▼
  KiteTickerStream        background thread, own asyncio loop
          │  decode_message()  -> kite_protocol
          ▼
  Tick -> QuoteData        tagged DataQuality.LIVE
          │
          ▼
  TickService              throttled persist + broadcast
     ├──► quotes table     at most once per 5s per security
     └──► dashboard        via Redis pub/sub -> browser WebSocket
```

Ticks arrive several times a second per instrument. Writing each one would
swamp PostgreSQL for no analytical gain — the signal engine reads the latest
quote, not a tick history — so database writes are throttled per security
while the in-memory latest price stays exact and unthrottled
(`TickService.latest()`).

## Operational notes

**Run the stream in exactly one process.** The worker owns it; the API service
has `KITE_STREAMING_ENABLED=false` in compose. Two connections sharing one
access token will be refused by Zerodha.

**Reconnection.** Backs off exponentially to 60s and resubscribes on
reconnect — Kite keeps no server-side memory of a dropped connection's
subscriptions, so without that, silence after a reconnect would look exactly
like a quiet market.

**Token expiry is not retried.** A 403 on the handshake means the token is
dead; reconnecting cannot fix it, so the stream stops and records
`kite_auth_failed`. Re-login and restart the worker.

**Silence detection.** A live Kite socket sends a heartbeat roughly every
second. Thirty seconds of silence is treated as a wedged connection and
triggers a reconnect, because a socket that is open but mute is the failure
mode most likely to go unnoticed.

**Instrument limit.** 3000 per connection. Beyond that the adapter logs a
refusal rather than silently dropping subscriptions.

## Deliberately not implemented: order placement

Kite is a **broker** API. The same credentials that stream quotes can place,
modify and cancel real orders.

This adapter is **market data only**. No order endpoint is called anywhere in
it, and order-update frames arriving on the websocket are logged and ignored.

Trading through Zerodha would mean implementing
`app.trading.brokers.base.Broker` and enabling live mode — which requires
`TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true` and a connected adapter, all
three. That is a separate decision about real money, and it is not made here.

## Testing

```bash
pytest tests/unit/test_kite_adapter.py          # protocol, REST, stream logic
pytest tests/integration/test_kite_stream_live.py  # against a local Kite-protocol server
```

The integration suite stands a WebSocket server in front of the adapter that
emits genuine Kite-format binary frames, exercising the background thread, the
subscription handshake, reconnection with resubscription, and clean shutdown.

**What has not been tested:** the live `wss://ws.kite.trade` endpoint. That
needs a paid subscription and credentials. The binary decoder is verified
against the documented packet layouts, but if Zerodha changes the wire format,
that is where it will show — `kite_protocol.py` refuses unrecognised packet
lengths rather than guessing at a layout, so a change fails loudly instead of
producing plausible wrong prices.
