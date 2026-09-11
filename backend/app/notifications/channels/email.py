"""SMTP email channel.

Works with any SMTP provider the operator configures. Nothing is sent -- and
no error is raised -- when SMTP credentials are absent; the channel simply
reports itself unconfigured so the dispatcher can skip it.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings
from app.core.logging import get_logger
from app.notifications.channels.base import DeliveryResult, NotificationChannel

log = get_logger(__name__)


class EmailChannel(NotificationChannel):
    name = "email"

    def is_configured(self) -> bool:
        return bool(settings.smtp_host and settings.smtp_from)

    def send(self, *, title, body, payload=None, recipient=None) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(self.name, False, "SMTP is not configured")
        if not recipient:
            return DeliveryResult(self.name, False, "no recipient address")

        message = EmailMessage()
        message["Subject"] = title[:200]
        message["From"] = settings.smtp_from
        message["To"] = recipient
        message.set_content(body)

        try:
            if settings.smtp_use_tls:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
                    server.starttls(context=ssl.create_default_context())
                    if settings.smtp_username:
                        server.login(settings.smtp_username, settings.smtp_password or "")
                    server.send_message(message)
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
                    if settings.smtp_username:
                        server.login(settings.smtp_username, settings.smtp_password or "")
                    server.send_message(message)
            return DeliveryResult(self.name, True, f"sent to {recipient}")
        except Exception as exc:
            log.error("email_send_failed", error=str(exc))
            return DeliveryResult(self.name, False, str(exc)[:200])
