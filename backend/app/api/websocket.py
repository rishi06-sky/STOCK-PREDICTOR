"""WebSocket hub.

Keeps a registry of connected dashboard clients and fans out events. Redis
pub/sub carries messages between processes, so an alert raised by the worker
reaches a client connected to the API process.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

CHANNEL = "stockintel:events"


class ConnectionManager:
    def __init__(self):
        self._connections: set = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        log.info("websocket_connected", clients=len(self._connections))

    async def disconnect(self, websocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, message: dict) -> int:
        payload = json.dumps(
            {**message, "ts": datetime.now(timezone.utc).isoformat()}, default=str
        )
        async with self._lock:
            targets = list(self._connections)

        delivered = 0
        for websocket in targets:
            try:
                await websocket.send_text(payload)
                delivered += 1
            except Exception:
                await self.disconnect(websocket)
        return delivered


manager = ConnectionManager()


def _redis_client():
    try:
        import redis

        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:  # pragma: no cover - optional dependency path
        log.warning("redis_unavailable", error=str(exc))
        return None


def broadcast_sync(message: dict) -> None:
    """Publish from synchronous code (workers, request handlers).

    Publishing to Redis rather than touching the socket set directly is what
    lets a worker process reach clients connected to the API process.
    """
    client = _redis_client()
    if client is None:
        return
    try:
        client.publish(
            CHANNEL,
            json.dumps({**message, "ts": datetime.now(timezone.utc).isoformat()}, default=str),
        )
    except Exception as exc:
        log.warning("broadcast_publish_failed", error=str(exc))
    finally:
        try:
            client.close()
        except Exception:
            pass


async def redis_subscriber() -> None:
    """Relay Redis pub/sub messages to connected WebSocket clients."""
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        log.warning("redis_asyncio_unavailable")
        return

    while True:
        try:
            client = aioredis.from_url(settings.redis_url, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            log.info("websocket_relay_started", channel=CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await manager.broadcast(json.loads(message["data"]))
                except Exception as exc:
                    log.warning("relay_decode_failed", error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("websocket_relay_error", error=str(exc))
            await asyncio.sleep(5)
