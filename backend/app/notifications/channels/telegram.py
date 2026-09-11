"""Telegram bot channel.

Uses the public Bot API. Free to operate: the user creates a bot via BotFather
and supplies the token and chat id.
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.notifications.channels.base import DeliveryResult, NotificationChannel

log = get_logger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramChannel(NotificationChannel):
    name = "telegram"

    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def send(self, *, title, body, payload=None, recipient=None) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(self.name, False, "Telegram bot is not configured")

        chat_id = recipient or settings.telegram_chat_id
        text = f"*{_escape(title)}*\n\n{_escape(body)}"
        try:
            response = httpx.post(
                API_URL.format(token=settings.telegram_bot_token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
                timeout=15,
            )
            if response.status_code == 200 and response.json().get("ok"):
                return DeliveryResult(self.name, True, f"sent to chat {chat_id}")
            return DeliveryResult(
                self.name, False, f"HTTP {response.status_code}: {response.text[:150]}"
            )
        except Exception as exc:
            log.error("telegram_send_failed", error=str(exc))
            return DeliveryResult(self.name, False, str(exc)[:200])


def _escape(text: str) -> str:
    """Escape MarkdownV2 reserved characters."""
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text
