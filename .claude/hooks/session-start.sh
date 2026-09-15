#!/usr/bin/env bash
# SessionStart hook: make a fresh container able to run the test suite, the
# linters and the app itself.
#
# Claude Code on the web starts each session in a new container. Nothing from
# the previous one survives except the repository checkout, so without this the
# first thing any session has to do is rebuild the environment by hand.
#
# Everything here is idempotent: on a warm container each step finds its work
# already done and returns in a second or two.
set -euo pipefail

# Local machines already have a working environment; only the remote container
# needs rebuilding. Remove this guard if you want it everywhere.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

log() { printf '[session-start] %s\n' "$*"; }

# --------------------------------------------------------------- services
# PostgreSQL and Redis are installed in the image but not running. The data
# directory survives a restart, so this reconnects to existing data rather
# than creating any.
log "starting PostgreSQL and Redis"
./scripts/dev-services.sh

# The role and databases are not part of the image. Creating them is safe to
# repeat -- each step checks first -- and it is what makes the README's
# "Local development (no Docker)" path work on a clean machine.
if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='stockintel'\"" | grep -q 1; then
  log "creating the stockintel role"
  su postgres -c "psql -q -c \"CREATE ROLE stockintel LOGIN PASSWORD 'stockintel' CREATEDB\""
fi

for db in stockintel stockintel_test; do
  if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$db'\"" | grep -q 1; then
    log "creating database $db"
    su postgres -c "createdb -O stockintel $db"
  fi
done

# --------------------------------------------------------------- backend
if [ ! -x backend/.venv/bin/python ]; then
  log "creating the backend virtualenv"
  python3 -m venv backend/.venv
fi

# pip resolves to "already satisfied" on a warm container, so this is cheap to
# repeat and still picks up a changed requirements.txt.
log "installing backend dependencies"
backend/.venv/bin/pip install --quiet --disable-pip-version-check -r backend/requirements.txt

# --------------------------------------------------------------- frontend
# install rather than ci: the container image is cached after this hook
# completes, and install can reuse what is already in node_modules.
log "installing frontend dependencies"
(cd frontend && npm install --silent --no-audit --no-fund)

# --------------------------------------------------------------- schema
# Migrations are idempotent and fast. Applying them here means a session can
# run the integration and e2e tests immediately, and it keeps a container that
# was cached before a new migration landed from starting up stale.
log "applying migrations"
(
  cd backend
  DATABASE_URL="postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel" \
    .venv/bin/alembic upgrade head >/dev/null
)

# Reference data (markets, exchanges, securities, indices) upserts, so this
# neither duplicates rows nor overwrites ingested prices.
log "seeding reference data"
(
  cd backend
  DATABASE_URL="postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel" \
  ENVIRONMENT=development \
    .venv/bin/python scripts/seed.py >/dev/null
)

# --------------------------------------------------------------- session env
# Written to CLAUDE_ENV_FILE so every command in the session inherits them and
# nobody has to remember that the checked-in .env points at Docker hostnames
# (postgres:5432, redis:6379) that do not resolve outside compose.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo 'export DATABASE_URL="postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel"'
    echo 'export REDIS_URL="redis://localhost:6379/0"'
    echo 'export ENVIRONMENT="development"'
    # No market-data host is reachable from the sandbox, so the only provider
    # that can return anything is the test fixture. Everything it produces is
    # tagged SYNTHETIC end to end and is never presented as real market data.
    echo 'export MARKET_DATA_PROVIDERS="fixture"'
    echo 'export ENABLE_FIXTURE_PROVIDER="true"'
  } >> "$CLAUDE_ENV_FILE"
fi

# --------------------------------------------------------------- servers
# Bring the app back up. Each restart otherwise leaves the API and dashboard
# down until someone notices and restarts them by hand.
#
# Started with nohup so they outlive this hook, and only when the port is
# actually free -- on a warm container the previous pair may still be running,
# and a second uvicorn would just fail to bind.
start_server() {
  local name="$1" port="$2" dir="$3"
  shift 3
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    exec 3>&-
    log "$name already listening on $port"
    return 0
  fi
  log "starting $name on $port"
  (
    cd "$dir"
    nohup "$@" > "/tmp/stockintel-$name.log" 2>&1 &
  )
}

start_server api 8000 backend \
  env DATABASE_URL="postgresql+psycopg://stockintel:stockintel@localhost:5432/stockintel" \
      REDIS_URL="redis://localhost:6379/0" \
      ENVIRONMENT=development \
      MARKET_DATA_PROVIDERS=fixture \
      ENABLE_FIXTURE_PROVIDER=true \
      .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# dev rather than start: next start needs a production build, and building
# here would add a minute to every session for a server nobody may use.
start_server web 3000 frontend npm run dev

log "ready"
