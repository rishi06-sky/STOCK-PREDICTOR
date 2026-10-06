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

**`models` is UNHEALTHY: "production model(s) unusable".**
The registry row and the artefact on disk have diverged — the file is missing,
or its bytes no longer match the checksum recorded when it was trained.
Predictions are disabled until this is resolved, which is the intended
fail-safe: an artefact that cannot be verified is never loaded. The health
detail names the version and the specific problem.

Causes, in order of likelihood: the model store was restored from a backup
without the database (or vice versa), the directory is not the one the running
process is configured to use (`ML_MODEL_DIR`), or a second process wrote to the
same store. Note that `save_model` refuses to overwrite an existing artefact,
so a live model cannot be clobbered by a normal retrain.

To recover, either:

* **Retrain.** `python scripts/bootstrap.py --skip-ingest` registers a new
  version and promotes it if it clears the gate. The gate does not hold a
  replacement to the score of an unusable incumbent — that model is serving
  nothing — but every absolute bar still applies, so a model with no edge is
  still refused.
* **Roll back.** `POST /api/v1/models/{id}/rollback` (admin) restores the most
  recent archived version *whose artefact verifies*, skipping any that do not.
  If none verify it returns nothing rather than restoring a broken model, and
  retraining is the only route.

Restoring the missing artefact from backup also works, if you have it: the
checksum in `model_versions.artifact_sha256` tells you whether the file you
found is the right one.

## Deploying

`.github/workflows/deploy.yml` deploys `main` to one server over SSH once CI
has passed on it. It can also be started by hand from the Actions tab (it
only deploys `main`). Until `DEPLOY_HOST` is set it skips itself with a
notice, so it does nothing on a repository with no server.

On the server it checks out the exact commit CI tested and runs
`scripts/deploy.sh`. That script builds the images, restarts the stack,
applies migrations, and then waits for the API to report a healthy
`database` and for the frontend to answer. The overall health status is
not used as the gate, because it is legitimately `DEGRADED` on a healthy
deploy (no promoted model yet, stale quotes over a weekend). The script
works by hand too: `git checkout --detach <sha> && ./scripts/deploy.sh`.

### One-time server setup

Requirements: an Ubuntu 22.04/24.04 or Debian 12 server with about 2 vCPU and
4 GB of RAM, and a domain whose DNS **A record** points at the server's IPv4
address. If the DNS is on Cloudflare, set that record to "DNS only" (grey
cloud): GitHub Actions connects to the domain over SSH, which Cloudflare's
proxy does not carry.

Then, as root on the server:

```bash
curl -fsSL https://raw.githubusercontent.com/rishi06-sky/STOCK-PREDICTOR/main/scripts/server-setup.sh -o server-setup.sh
less server-setup.sh              # read it before running it
bash server-setup.sh stocks.example.com
```

`scripts/server-setup.sh` installs Docker, allows only SSH, HTTP and HTTPS
through `ufw`, creates a `deploy` user, and clones the repository to
`/opt/stock-intelligence`. It writes a production `.env` with a generated
`SECRET_KEY` and database password, starts the stack, and loads reference
data. It also creates the SSH key GitHub Actions deploys with (limited with
`restrict` in `authorized_keys`), and finally prints every value for the
table below. It is safe to re-run: an existing `.env`, user, clone or key is
kept.

On a public server the stack runs with `COMPOSE_PROFILES=https`, which adds
Caddy on ports 80 and 443. Caddy obtains the HTTPS certificate for `DOMAIN`
and sends `/api/*` and `/ws` to the API and everything else to the frontend.
The API and frontend ports stay on `127.0.0.1`. That matters because Docker
publishes ports around `ufw`, and because the API's rate limiter trusts
`X-Forwarded-For`, which only Caddy sets reliably (it replaces whatever a
client sends).

Afterwards, register at `https://<domain>` (the first account becomes the
administrator) and load price history and the first model, which takes a
while:

```bash
sudo -u deploy -H bash -c 'cd /opt/stock-intelligence && docker compose exec -T api python scripts/bootstrap.py'
```

### Repository settings

Under **Settings > Secrets and variables > Actions**:

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_HOST` | secret | the domain |
| `DEPLOY_USER` | secret | `deploy` |
| `DEPLOY_SSH_KEY` | secret | the private key the setup script prints; delete it from the server afterwards |
| `DEPLOY_KNOWN_HOSTS` | secret | the host key line the setup script prints |
| `DEPLOY_PATH` | variable | the clone's path, e.g. `/opt/stock-intelligence` |
| `DEPLOY_PORT` | variable | SSH port, if not 22 |

The job runs in a `production` environment. To require someone to approve
each deploy, add required reviewers to that environment under
**Settings > Environments**.

### Rolling back

The deploy log prints the commit that was running before. On the server:

```bash
git checkout --detach <previous-sha> && ./scripts/deploy.sh
```

Migrations are not reversed automatically. If the release being rolled back
added one, downgrade it first with `docker compose exec api alembic downgrade -1`,
and only after checking that the downgrade is safe for the data.

## Emergency shutdown

1. **Kill switch** (immediate, reversible): System page → *Engage kill
   switch*, or `POST /api/v1/system/kill-switch?engage=true`. Blocks every new
   order in both paper and live mode. Runtime-only — set
   `KILL_SWITCH_ENGAGED=true` in `.env` to survive a restart.
2. **Stop automation**: `docker compose stop worker`. Ingestion, signals and
   trading halt; the dashboard stays readable.
3. **Full stop**: `docker compose down`.

## Backups

`scripts/deploy.sh` dumps the database before it migrates, into `backups/` on
the server, and refuses to migrate if the dump fails or comes back truncated
(it checks for pg_dump's completion marker, not a file size -- a small schema
legitimately compresses to a few hundred bytes). It keeps the last `BACKUP_KEEP`
dumps, default 10; `BACKUP_DIR` moves them elsewhere.

That covers the one step a deploy cannot undo by checking out the previous
commit. It is **not** an offsite backup and it does not include the model
store, so take those separately.

To restore the dump taken immediately before a bad migration:

```bash
ls -1t backups/                 # newest first, named <db>-<utc>-<commit>.sql.gz
docker compose stop api worker
gunzip -c backups/<file>.sql.gz | docker compose exec -T postgres psql -U stockintel stockintel
git checkout --detach <previous-commit> && ./scripts/deploy.sh
```

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
