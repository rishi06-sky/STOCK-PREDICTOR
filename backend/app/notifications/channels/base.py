"""Notification channel contract."""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(slots=True)
class DeliveryResult:
    channel: str
    delivered: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"channel": self.channel, "delivered": self.delivered, "detail": self.detail}


class NotificationChannel(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when this channel has everything it needs to deliver."""

    @abc.abstractmethod
    def send(self, *, title: str, body: str, payload: dict | None = None,
             recipient: str | None = None) -> DeliveryResult:
        ...

    def health_check(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "not configured"
        return True, "configured"
