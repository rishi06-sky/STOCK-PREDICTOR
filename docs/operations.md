# Operations runbook

## Daily rhythm

The worker process runs everything on a schedule; no manual step is required
after deployment.

| Job | Cadence | What it does |
|---|---|---|
| `ingest_quotes` | every 15 min | refresh the latest quote per security |
| `pipeline_cycle` | every 30 min | full cycle: ingest → features → predict → signal → risk → rank → alert → paper trade |
| `expire_signals` | every 10 min | expire signals past their TTL; invalidate those overtaken by price |
| `daily_snapshot` | 23:30 UTC | record the portfolio equity point |
| `retrain` | Sundays, 02:00 UTC | retrain candidates and run them through the promotion gate |

## Health

`GET /api/v1/health` — public, cheap, suitable for an uptime probe.
`GET /api/v1/health/full` — authenticated; also probes the providers over the
network.

Status is per component with a reason. An unknown component reports
`UNHEALTHY`, never `HEALTHY`.

| Component | `DEGRADED` means | `UNHEALTHY` means |
|---|---|---|
| `database` | — | unreachable; nothing works |
| `redis` | caching and live updates are off; the platform still functions | — |
| `data_freshness` | quotes are past the staleness window and treated as delayed | past the halt threshold — **signal generation stops** |
| `models` | no promoted model, or one older than 45 days | — |
| `signals` | none generated in 24h | — |
| `notifications` | more failures than successes in 24h | — |
| `trading` | kill switch engaged | — |
| `market_data_providers` | some providers unreachable | **all** unreachable; ingestion cannot run |

## Common situations

**No signals are being generated.**
Check `models` on the System page. If no model cleared the promotion gate,
this is deliberate: a model without a demonstrated edge must not drive
recommendations. Run `python scripts/bootstrap.py --years 6` to train on more
history.

**Data freshness is UNHEALTHY.**
Signal generation has halted by design. Check `market_data_providers` — if
every provider is unreachable, the cause is network or egress policy, not the
application. The platform will resume on its own once data flows.

**Alerts stopped arriving.**
Check `notifications` on the System page. Unconfigured channels report
themselves as such rather than failing silently. The in-app channel always
works, so alerts are never lost even when SMTP and Telegram are unset.

**Paper trading opened nothing.**
Read the `skipped` reasons in the cycle report. Common causes: the drawdown
circuit breaker, the sector concentration limit, insufficient cash, or every
signal sitting below the confidence floor. Each is a risk control working.

**A model got worse after retraining.**
It was not promoted — the gate compares candidates against the incumbent.
To revert manually: `POST /api/v1/models/{id}/rollback` (admin).

## Emergency shutdown

1. **Kill switch** (immediate, reversible): System page → *Engage kill
   switch*, or `POST /api/v1/system/kill-switch?engage=true`. Blocks every new
   order in both paper and live mode. Runtime-only — set
   `KILL_SWITCH_ENGAGED=true` in `.env` to survive a restart.
2. **Stop automation**: `docker compose stop worker`. Ingestion, signals and
   trading halt; the dashboard stays readable.
3. **Full stop**: `docker compose down`.

## Backups

Everything that matters is in PostgreSQL plus the model store.

```bash
docker compose exec postgres pg_dump -U stockintel stockintel | gzip > backup-$(date +%F).sql.gz
docker run --rm -v stock-intelligence_model_store:/m -v "$PWD":/b alpine \
  tar czf /b/models-$(date +%F).tar.gz -C /m .
```

Restore:

```bash
gunzip -c backup-2026-09-11.sql.gz | docker compose exec -T postgres psql -U stockintel stockintel
```

## Scaling

- The API is stateless — run several replicas behind a load balancer.
- Run **exactly one** worker. The scheduler is not distributed; two workers
  would duplicate ingestion and trading.
- The in-process rate limiter is per API process. With multiple replicas, move
  it to Redis so the budget is shared.
