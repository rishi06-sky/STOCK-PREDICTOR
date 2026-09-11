#!/usr/bin/env bash
# Start PostgreSQL and Redis for local development (non-Docker path).
set -uo pipefail

service postgresql start >/dev/null 2>&1
service redis-server start >/dev/null 2>&1

for _ in $(seq 1 20); do
  pg_isready -q && break
  sleep 0.5
done

pg_isready && echo "postgres: ready" || { echo "postgres: FAILED"; exit 1; }
[ "$(redis-cli ping 2>/dev/null)" = "PONG" ] && echo "redis: ready" || { echo "redis: FAILED"; exit 1; }
