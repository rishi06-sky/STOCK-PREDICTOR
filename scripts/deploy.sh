#!/usr/bin/env bash
# Roll the Docker Compose stack forward to the commit that is checked out.
#
# Run from the repository root on the server, after checking out the commit to
# deploy. The GitHub Actions deploy workflow does exactly that over SSH; it can
# also be run by hand:
#
#   git fetch origin && git checkout --detach <sha> && ./scripts/deploy.sh
#
# Steps: build the images, restart the services, apply database migrations,
# then wait until the API reaches its database and the frontend answers. It
# exits non-zero if any step fails, leaving the new containers running so the
# failure can be inspected with `docker compose logs`.
set -euo pipefail

cd "$(dirname "$0")/.."

log() { printf '[deploy] %s\n' "$*"; }

if [ ! -f .env ]; then
  log ".env is missing in $(pwd). Create it from .env.example and set SECRET_KEY first."
  exit 1
fi

# Compose reads COMPOSE_PROFILES and DOMAIN from .env; mirror that here so a
# public server cannot come up with the HTTPS entry point misconfigured.
env_value() { sed -n "s/^$1=//p" .env | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//'; }
https_enabled=false
case ",$(env_value COMPOSE_PROFILES)," in *,https,*) https_enabled=true ;; esac
if [ "$https_enabled" = true ] && [ -z "$(env_value DOMAIN)" ]; then
  log "COMPOSE_PROFILES includes https but DOMAIN is empty in .env"
  exit 1
fi

log "deploying $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

log "building images"
docker compose build --pull

log "starting services"
docker compose up -d --remove-orphans

log "applying migrations"
docker compose exec -T api alembic upgrade head

# The overall health status can legitimately be DEGRADED or UNHEALTHY on a
# healthy deploy (stale quotes over a weekend, no promoted model yet), so the
# check is narrower: the API answers and reaches its database.
log "waiting for the api"
api_ready=false
for _ in $(seq 1 30); do
  if docker compose exec -T api python - <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen("http://localhost:8000/api/v1/health", timeout=5) as response:
    body = json.load(response)
database = next(c for c in body["components"] if c["component"] == "database")
sys.exit(0 if database["status"] == "HEALTHY" else 1)
PY
  then
    api_ready=true
    break
  fi
  sleep 4
done
if [ "$api_ready" != true ]; then
  log "api did not report a healthy database within 2 minutes"
  docker compose logs --tail=50 api
  exit 1
fi

log "waiting for the frontend"
frontend_ready=false
for _ in $(seq 1 30); do
  if docker compose exec -T frontend wget -q -O /dev/null http://localhost:3000/login 2>/dev/null; then
    frontend_ready=true
    break
  fi
  sleep 4
done
if [ "$frontend_ready" != true ]; then
  log "frontend did not answer within 2 minutes"
  docker compose logs --tail=50 frontend
  exit 1
fi

if [ "$https_enabled" = true ]; then
  log "checking caddy"
  sleep 3
  if [ -z "$(docker compose ps --status running -q caddy)" ]; then
    log "caddy is not running"
    docker compose logs --tail=50 caddy
    exit 1
  fi
fi

log "removing dangling images"
docker image prune -f >/dev/null

log "done"
