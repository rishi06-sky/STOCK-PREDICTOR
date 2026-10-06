# Going live with real market data

Everything in this repository has been built and tested against the fixture
provider, whose output is tagged `SYNTHETIC` end to end. No model has ever seen
a real price, so the platform's central question -- does any model clear the
promotion gate on real data? -- is still open.

This runbook takes it from there. Work through it in order; each step has a
check that must pass before the next one is worth attempting.

---

## 0. Prerequisites (not code)

Two things block every step below and neither can be worked around from inside
the application.

**Network egress.** The three market-data hosts must be reachable:

```bash
for h in query1.finance.yahoo.com stooq.com www.alphavantage.co; do
  printf '%-28s ' "$h"
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}\n' "https://$h/"
done
```

Every line must print a real HTTP status. `000` means the connection never
completed -- behind a filtering proxy that is a policy denial, and the fix is an
allowlist entry, not a retry.

**An Alpha Vantage key.** Free tier: <https://www.alphavantage.co/support/#api-key>

```bash
# in the root .env -- never in .env.example, which is tracked
ALPHA_VANTAGE_API_KEY=your-key-here
MARKET_DATA_PROVIDERS=yahoo,stooq,alpha_vantage
```

Alpha Vantage belongs **last** in the chain. Its free tier allows roughly 25
requests per day, so it is a fallback for symbols the free providers could not
serve, not a primary feed. Note also that it publishes no NSE listings (see the README), so most of this universe will fall through to Yahoo.

Verify the key is readable without printing it. Run it from the repository
root with the project's interpreter -- `.env` is resolved relative to the
working directory, and the system python has no dependencies installed:

```bash
backend/.venv/bin/python -c "import sys; sys.path.insert(0,'backend'); \
  from app.core.config import Settings; s=Settings(); \
  print('chain:', s.provider_chain); \
  print('key present:', bool(s.alpha_vantage_api_key))"
```

**An exported environment variable beats `.env`.** That is ordinary
pydantic-settings precedence, but it bites here: a shell (or a container, or
the session-start hook) that exports `MARKET_DATA_PROVIDERS=fixture` silently
wins over a correctly edited file, and the platform keeps serving synthetic
data while everything looks configured. The command above reports the
*effective* chain, which is why it is the check that matters. If it prints
something other than what `.env` says, look here first:

```bash
env | grep -E '^(MARKET_DATA_PROVIDERS|ENABLE_FIXTURE_PROVIDER|ENVIRONMENT)='
```

---

## 1. Ingest real history

`bootstrap.py` runs ingestion, training and one pipeline cycle in a single
pass. Point it at a real provider chain rather than the fixture:

```bash
docker compose exec api python scripts/bootstrap.py --years 4
```

Or locally, from `backend/`:

```bash
ENV_FILE=../.env MARKET_DATA_PROVIDERS=yahoo,stooq,alpha_vantage \
  .venv/bin/python scripts/bootstrap.py --years 4
```

**Check before continuing.** The `[2/4]` line reports bars written and any
securities that failed. Then confirm nothing synthetic remains:

```sql
SELECT quality, provider, count(*) FROM price_data GROUP BY 1, 2;
```

Expect `EOD` or `DELAYED` rows from `yahoo`/`stooq`. If `SYNTHETIC`/`fixture`
rows are still present the run used the wrong chain -- clear them before
training, or the model learns a blend of real and invented prices:

```sql
DELETE FROM price_data WHERE quality = 'SYNTHETIC';
```

Also check for ingestion damage that the validator caught:

```sql
SELECT symbol, count(*) FROM price_data p
  JOIN securities s ON s.id = p.security_id
 GROUP BY 1 ORDER BY 2;
```

A security with far fewer bars than its peers usually means the provider
returned partial history, not that the stock is young.

---

## 2. Train, and read the gate honestly

`bootstrap.py` already trains and evaluates at `[3/4]`. Every candidate prints
its cross-validated AUC, and the gate prints either `PROMOTED` or
`NOT PROMOTED` with the specific thresholds it failed.

The gate (`backend/app/ml/registry.py`):

| Threshold | Value |
|---|---|
| `min_roc_auc` | 0.52 |
| `min_lift_over_baseline` | 0.005 |
| `max_calibration_error` | 0.15 |
| `min_training_rows` | 500 |
| `max_auc_regression` | 0.02 |

**Three outcomes, and all three are legitimate results.**

**A model is promoted.** Signals will start appearing after the next pipeline
cycle. Before trusting them, confirm the leakage canary still holds -- it
asserts a model scores below 0.56 AUC on a pure random walk:

```bash
cd backend && .venv/bin/python -m pytest tests/unit/test_ml_integrity.py -q
```

**Nothing clears the gate.** This is a real finding, not a bug. Daily direction
prediction on large-cap equities from price-derived features is genuinely hard,
and ~0.52 AUC is close to what the literature would lead you to expect. The
platform is behaving exactly as designed: it reports no edge rather than
inventing one. Reasonable next moves are a longer history (`--years 6`), a
different horizon (`--horizons 10,21`), or better features -- not lowering the
gate. Lowering the gate does not create an edge, it just stops you finding out
that there isn't one.

**A model scores unusually well.** Treat anything above roughly 0.60 AUC as
suspect until proven otherwise. That is the range where leakage normally shows
up, and this project has already had one instance: a 0.6001 model that turned
out to be learning splices left by an ingestion bug. Check the canary, check
for discontinuities, and re-read the feature set for anything that could see
the future:

```sql
-- large single-session moves are the fingerprint of a bad splice
SELECT s.symbol, p.trade_date, p.close
  FROM price_data p JOIN securities s ON s.id = p.security_id
 ORDER BY p.security_id, p.trade_date;
```

---

## 3. Backtest

Only meaningful once a model is promoted. A backtest of an unpromoted model
measures nothing.

Run one from the UI (Backtests) or the API, and compare its assumptions against
`COMMISSION_BPS` and `SLIPPAGE_BPS` in `.env` -- a backtest that ignores costs
will look better than the paper portfolio ever will.

---

## 4. Before real money

Live trading is disabled by default and should stay that way until every line
below is true.

- [ ] A model is promoted **and** the leakage canary passes
- [ ] A backtest covers at least one full market cycle, costs included
- [ ] Paper trading has run for a meaningful stretch and its P&L is
      explainable -- every trade traceable to a signal
- [ ] `SECRET_KEY` is a real secret, `DEBUG=false`, `CORS_ORIGINS` is not `*`
- [ ] Database backups exist and a restore has been tested
- [ ] The kill switch has been exercised at least once (`KILL_SWITCH_ENGAGED=true`)

`LIVE_TRADING_ENABLED=true` additionally requires `TRADING_MODE=live`; the
config refuses to boot otherwise. That refusal is a safety feature and should
not be edited around.
