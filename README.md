# Stock Intelligence Platform

AI-assisted market monitoring, signal generation and paper trading on
India's **NSE** and **BSE**, settling in **INR**.

The seeded universe is deliberately single-currency. Carrying US listings
alongside Indian ones meant one portfolio held both INR and USD positions, and
valuing it summed them without converting — rupees added to dollars. The free
data path has no FX source to convert with honestly, so the scope is one
market. The adapter layer and exchange tables are unchanged, so adding a
venue back is a seeding change, not a rewrite.

The system ingests market data, engineers features, trains and validates
models under walk-forward cross-validation, generates risk-checked signals
with explanations, ranks opportunities, raises alerts and trades them in a
paper portfolio — automatically, on a schedule.

> **Not investment advice.** Signals are model estimates carrying real
> uncertainty. The platform is built to be honest about that: it reports the
> majority-class baseline beside every accuracy figure, refuses to emit a
> signal when confidence is below the floor, and halts rather than guessing
> when data is stale.

---

## What makes this trustworthy

Most of the engineering here is spent on *not* being wrong:

| Property | How it is enforced |
|---|---|
| **No look-ahead bias** | Every indicator is causal, verified by recomputing on truncated series. Labels are forward-shifted and the trailing unlabelled rows are dropped. |
| **No data leakage** | Walk-forward CV splits on unique *dates* with a trading-day embargo between train and test. The holdout block is scored exactly once. |
| **Proven, both ways** | On a pure random walk every model family scores ~0.49 AUC and is refused promotion. On data with an injected momentum effect the same pipeline reaches 0.62–0.65. A pipeline that always returned 0.5 would fail the second test. |
| **No fake freshness** | Every price carries `LIVE` / `DELAYED` / `EOD` / `SYNTHETIC`. Delayed data is never presented as real-time. |
| **Fail-safe** | Stale data halts signal generation. An unknown price refuses a trade. A missing validated model produces no signals at all. |
| **No false claims** | Accuracy is always shown against the majority-class baseline, with Brier score and calibration error. A model that cannot beat the baseline is never promoted. |
| **Risk is not bypassable** | Every order passes kill switch → idempotency → risk engine → broker → ledger, in that order, by construction. |
| **Live trading is off** | Requires `TRADING_MODE=live` **and** `LIVE_TRADING_ENABLED=true` **and** a configured broker adapter. |

---

## Architecture

```
              MARKET DATA PROVIDERS
   Yahoo Finance · Stooq · Alpha Vantage · Finnhub
                      │  (adapter layer with failover)
                      ▼
            INGESTION → VALIDATION → PostgreSQL
                      │
                      ▼
              FEATURE ENGINEERING (35 stationary features)
          ┌───────────┼───────────┐
          ▼           ▼           ▼
     Technical   Fundamental    News/Sentiment
          └───────────┼───────────┘
                      ▼
        ML PREDICTION (walk-forward validated, calibrated)
                      ▼
             MARKET REGIME DETECTION
                      ▼
                 SIGNAL ENGINE  ──► NO_ACTION below the confidence floor
                      ▼
                  RISK ENGINE   ──► sizing, exposure, drawdown, sector limits
                      ▼
             OPPORTUNITY RANKING
          ┌───────────┼───────────┐
          ▼           ▼           ▼
      Dashboard    Alerts    Paper Trading
                                  ▼
                          Broker Adapter (live: disabled by default)
```

**Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Redis · APScheduler ·
scikit-learn / XGBoost / LightGBM · Next.js 15 · TypeScript · Tailwind ·
Docker Compose.

The backend is **synchronous** SQLAlchemy with FastAPI `def` endpoints, which
run in a threadpool. This avoids async/sync bridging around pandas and
scikit-learn entirely — the simpler, more reliable choice at this scale.

---

## Quick start (Docker)

```bash
cp .env.example .env
# REQUIRED: generate a secret
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env

docker compose up -d --build
docker compose exec api alembic upgrade head     # create the schema
docker compose exec api python scripts/seed.py   # markets, exchanges, universe
```

Open <http://localhost:3000>. **The first account you register becomes the
administrator.**

Populate data and train the first model (the scheduler will then keep it
current on its own):

```bash
docker compose exec api python scripts/bootstrap.py
```

### Stopping

```bash
docker compose stop                 # stop, keep data
docker compose down                 # stop and remove containers
docker compose down -v              # ALSO DELETES ALL DATA
```

---

## Local development (no Docker)

```bash
./scripts/dev-services.sh           # start PostgreSQL and Redis

# once per machine: the role and databases the app and the tests connect as
sudo -u postgres psql -c "CREATE ROLE stockintel LOGIN PASSWORD 'stockintel' CREATEDB"
sudo -u postgres createdb -O stockintel stockintel
sudo -u postgres createdb -O stockintel stockintel_test

cd backend
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/alembic upgrade head
./.venv/bin/python scripts/seed.py
./.venv/bin/uvicorn app.main:app --reload --port 8000

# separate terminal: the scheduler
./.venv/bin/python -m app.workers.run

# separate terminal: the frontend
cd frontend && npm install && npm run dev
```

The checked-in `.env` points at the Docker Compose hostnames (`postgres:5432`,
`redis:6379`), which do not resolve outside compose. Outside it, export
`DATABASE_URL=postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel`
and `REDIS_URL=redis://localhost:6379/0`.

API docs at <http://localhost:8000/docs> (disabled in production).

### Claude Code on the web

`.claude/hooks/session-start.sh` runs every one of the above steps
automatically at session start, then brings the API and dashboard up. Sessions
get a fresh container, so without it each one begins by rebuilding the
environment by hand. The hook is idempotent and exits immediately when
`CLAUDE_CODE_REMOTE` is unset, so it does nothing on a local machine.

It runs **asynchronously**: the session starts straight away and the hook
continues in the background — a few seconds on a warm container, longer on a
cold one where pip has to build the scientific stack. The cost of that is a
race: a command issued in the first moments of a session can beat the setup.
If something fails with a missing module, a missing table, or a refused
connection on port 5432, check `/tmp/stockintel-session-start.log` and wait for
its final `ready` line. To trade startup latency for certainty instead, delete
the `echo '{"async": ...}'` line from the hook and it becomes synchronous.

---

## Data providers

NSE and BSE publish **no public REST API**. Indian market data legitimately
requires a licensed vendor or a broker feed. Yahoo Finance's `.NS` / `.BO`
symbols are the only zero-cost path that also covers US markets, so it is the
default primary provider.

| Provider | Key | Coverage | Freshness | Cost |
|---|---|---|---|---|
| **Zerodha Kite Connect** | yes | NSE, BSE | **LIVE** — exchange-licensed WebSocket ticks | ~Rs 2,000/mo ([setup](docs/kite-streaming.md)) |
| **Yahoo Finance** | none | NSE, BSE (`.NS` / `.BO`) | ~15-min delayed intraday; reliable EOD | free, unofficial endpoint |
| **Stooq** | none | US + international | **EOD only** | courtesy use |
| Alpha Vantage | yes | global + fundamentals | 15-min delayed | ~25 requests/**day** |
| Finnhub | yes | US only + news | ~20-min delayed | ~60/min |

Providers sit behind an adapter interface with an ordered failover chain, so
swapping in a paid feed is a configuration change, not a rewrite.

**Real-time vs. real data.** On the free path the platform runs on *real*
market data that is *not* real-time: Yahoo is ~15 minutes behind and the
poller adds up to another 15, so a price on screen can be half an hour old.
Nothing ever emits `LIVE` on that path. Adding Zerodha Kite Connect replaces
polling with a pushed, exchange-licensed tick stream — sub-second, tagged
`LIVE`, NSE and BSE only. See [docs/kite-streaming.md](docs/kite-streaming.md).

### Configuring Alpha Vantage

1. **Get a key.** https://www.alphavantage.co/support/#api-key — an email
   address, no card. The key is issued on the page immediately.

2. **Put it in `.env`** (not `.env.example`, which is tracked in git):

   ```bash
   ALPHA_VANTAGE_API_KEY=your-key-here
   ```

   The variable name is fixed by `Settings.alpha_vantage_api_key`; pydantic
   reads it case-insensitively with no prefix.

3. **Add it to the chain.** Setting the key alone does nothing — the chain is
   built strictly from `MARKET_DATA_PROVIDERS`:

   ```bash
   MARKET_DATA_PROVIDERS=yahoo,stooq,alpha_vantage
   ```

   Order matters: the chain stops at the first provider returning valid data.
   Last position reserves the daily quota for symbols the free providers could
   not serve. Putting it first spends that quota on symbols Yahoo already
   covers.

4. **Verify without printing the secret:**

   ```bash
   python -c "import sys; sys.path.insert(0,'backend'); \
     from app.core.config import Settings; \
     from app.market_data.providers.alpha_vantage import AlphaVantageProvider; \
     s=Settings(); print('chain:', s.provider_chain); \
     print('key present:', bool(s.alpha_vantage_api_key)); \
     print('configured:', AlphaVantageProvider().is_configured())"
   ```

   The System page's `market_data_providers` health check reports the same
   thing at runtime: an unconfigured provider says so explicitly rather than
   failing silently.

**Run this from the repository root.** `env_file` is a relative path, so
running from `backend/` looks for `backend/.env`, which does not exist, and
your key is silently ignored. Use `ENV_FILE=../.env`, or `docker compose up`,
which passes the variable through explicitly.

**What Alpha Vantage can and cannot replace.** It implements `daily`, `quote`
and `fundamentals` only — no `intraday`, `news` or `search`. It is not a
substitute for Yahoo on the free path, and it is not a substitute for Kite at
all: Kite is the only provider here that emits `LIVE`, exchange-licensed ticks.
Treat Alpha Vantage as a fallback and a fundamentals source.

**Limitations, stated plainly:**

- Alpha Vantage's free tier is ~25 requests per **day**. A single full ingest
  over a 40-symbol universe exceeds it. `ALPHA_VANTAGE_RATE_LIMIT_PER_MINUTE`
  paces requests client-side but cannot raise the vendor's daily cap; once
  exhausted the API returns HTTP 200 with a prose `Note` body, which the
  adapter detects and surfaces as a rate-limit error rather than parsing as
  data.
- Yahoo's endpoint is undocumented, has no SLA, and can change without notice.
  Review its terms before commercial use.
- Free tiers are **delayed**. This is a research and paper-trading tool, not a
  low-latency trading system.
- Fundamentals coverage is thin on the free path. Missing fundamentals stay
  missing; nothing is estimated or filled in.

---

## Operating the platform

### Paper trading

Enabled by default. It uses the **same** signal and risk engines a live
deployment would, so results are a genuine rehearsal rather than a separate
toy path. Run a cycle manually from the Portfolio page, or let the scheduler
do it every `PIPELINE_INTERVAL_MINUTES`.

### Enabling live broker trading

Live trading is **disabled by default and deliberately awkward to enable**.
All three conditions must hold:

1. `TRADING_MODE=live`
2. `LIVE_TRADING_ENABLED=true`
3. A broker adapter implementing `app.trading.brokers.base.Broker`, connected

No live broker adapter ships with this project. Implementing one means
subclassing `Broker` and registering it — the order manager, risk engine and
idempotency guards are already broker-agnostic.

Before enabling, understand that every risk limit in `.env` becomes a real
constraint on real money, and that free delayed data is **not** suitable for
live execution.

### Emergency shutdown

| Urgency | Action |
|---|---|
| **Immediate** | System page → **Engage kill switch**. Blocks every new order at once, paper and live. |
| **Via API** | `POST /api/v1/system/kill-switch?engage=true` (admin) |
| **Across restarts** | Set `KILL_SWITCH_ENGAGED=true` in `.env`, then `docker compose restart api worker` |
| **Stop all automation** | `docker compose stop worker` — halts ingestion, signals and trading; the dashboard stays readable |
| **Full stop** | `docker compose down` |

The kill switch is checked before every order is placed, in both modes.

---

## Testing

```bash
cd backend
./.venv/bin/pytest                       # everything
./.venv/bin/pytest tests/unit            # fast, no database
./.venv/bin/pytest -m "not slow"         # skip model training
./.venv/bin/pytest --cov=app             # with coverage
```

Tests run against a separate `stockintel_test` database and refuse to start if
`ENVIRONMENT` is not `test` or the database URL does not name a test database.

---

## Costs

The default configuration costs **nothing** in services. Running it is a
matter of hosting:

| Item | Cost |
|---|---|
| Yahoo Finance, Stooq | free (unofficial / courtesy) |
| PostgreSQL, Redis | free (self-hosted) |
| Alpha Vantage, Finnhub | free tiers; optional |
| Email alerts | free tier of any SMTP provider |
| Telegram alerts | free |
| Hosting | a 2 vCPU / 4 GB VPS is sufficient (~$12–24/month) |

If a provider's free tier is exceeded, that adapter reports the rate limit and
the chain falls through to the next one. Nothing silently degrades to invented
data.

---

## Security

- Passwords hashed with bcrypt, pre-hashed with SHA-256 so long passphrases
  keep their entropy instead of being truncated at 72 bytes
- JWT access and refresh tokens with distinct types; API keys stored only as
  SHA-256 digests and shown once at creation
- Account lockout after repeated failures; tighter rate limits on credential
  endpoints
- Login and registration shaped so neither can enumerate accounts
- Security headers, and an error handler that keeps stack traces in the log
  rather than the response body
- Append-only audit log of authentication and every order
- Production refuses to boot without an explicit `SECRET_KEY`, with `DEBUG`
  on, with wildcard CORS, or with the test fixture provider enabled

Report security issues privately rather than via a public issue.

---

## Repository layout

```
backend/
  app/
    api/          FastAPI routes, dependencies, middleware, WebSocket hub
    core/         config, logging, exceptions, security primitives
    database/     engine, session, base, seed data
    models/       SQLAlchemy models (29 tables)
    market_data/  provider adapters, ingestion, validation, market hours
    technical/    indicators and the feature pipeline
    fundamentals/ ratio analysis and peer comparison
    news/         news ingestion and deduplication
    sentiment/    financial lexicon analyser
    ml/           dataset, validation, models, trainer, registry, prediction
    backtesting/  event-driven engine and performance metrics
    signals/      signal engine
    risk/         sizing and limits
    portfolio/    valuation, allocation, correlation
    trading/      broker abstraction, order manager, paper engine
    notifications/ dispatcher and channels
    monitoring/   health checks
    services/     pipeline orchestration
    workers/      scheduler and worker entrypoint
  alembic/        migrations
  tests/          unit, integration, e2e
frontend/
  src/app/        pages
  src/components/ shared UI
  src/lib/        API client and formatters
docker/           Dockerfiles
docs/             architecture and operations notes
scripts/          service helpers
```

---

## Licence and responsibility

Provided as-is for research and education. Markets carry risk; you are
responsible for any capital you place at risk and for complying with the terms
of any data provider or broker you connect.
