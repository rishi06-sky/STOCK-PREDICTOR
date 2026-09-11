"""In-app channel.

Delivery here means the alert row is persisted and broadcast over the
WebSocket hub so open dashboards update immediately. Always available, and the
fallback that guarantees no alert is silently lost when SMTP or Telegram are
unconfigured.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.notifications.channels.base import DeliveryResult, NotificationChannel

log = get_logger(__name__)


class InAppChannel(NotificationChannel):
    name = "in_app"

    def is_configured(self) -> bool:
        return True

    def send(self, *, title, body, payload=None, recipient=None) -> DeliveryResult:
        from app.api.websocket import broadcast_sync

        broadcast_sync(
            {"type": "alert", "title": title, "body": body, "payload": payload or {}}
        )
        return DeliveryResult(self.name, True, "stored and broadcast")
