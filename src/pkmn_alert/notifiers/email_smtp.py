"""SMTP email notifier — pairs well with Gmail + app passwords.

To use with Gmail:
  1. Turn on 2-Step Verification for your Google account.
  2. Create an App Password at https://myaccount.google.com/apppasswords
     (labelled something like "pkmn-alert").
  3. Set env vars ``GMAIL_USER=you@gmail.com`` and
     ``GMAIL_APP_PASSWORD=<the 16-char app password>``.
  4. Reference them in subscribers.yaml via ``${GMAIL_USER}`` /
     ``${GMAIL_APP_PASSWORD}`` — see ``subscribers.example.yaml``.

Any other SMTP provider (Fastmail, iCloud, custom) works the same way; just
override ``host`` / ``port`` in the channel options.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from ..event import DropEvent
from .base import Notifier

log = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.host: str = options.get("host", "smtp.gmail.com")
        self.port: int = int(options.get("port", 587))
        self.username: str = options.get("username") or ""
        self.password: str = options.get("password") or ""
        self.sender: str = options.get("from") or self.username
        self.recipient: str = options.get("to") or ""
        self.use_starttls: bool = bool(options.get("starttls", True))
        self.label = f"email[{self.recipient or '<unset>'}]"

    def is_configured(self) -> bool:
        missing = [
            name for name, val in (
                ("username", self.username),
                ("password", self.password),
                ("to", self.recipient),
            ) if not val
        ]
        if missing:
            log.debug("%s missing %s; skipping", self.label, ",".join(missing))
            return False
        return True

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = f"[Pokémon] {event.kind.upper()}: {event.title[:120]}"
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(_format_body(event))

        try:
            if self.use_starttls:
                with smtplib.SMTP(self.host, self.port, timeout=20) as srv:
                    srv.ehlo()
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                    srv.login(self.username, self.password)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=20) as srv:
                    srv.login(self.username, self.password)
                    srv.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("%s SMTP send failed for subscriber %s: %s", self.label, subscriber_id, exc)
            return False

        log.info("%s -> subscriber=%s ok", self.label, subscriber_id)
        return True


def _format_body(event: DropEvent) -> str:
    lines = [event.title, ""]
    if event.url:
        lines += [event.url, ""]
    lines += [
        f"kind:      {event.kind}",
        f"retailer:  {event.retailer}",
        f"region:    {event.region}",
        f"source:    {event.source}",
        f"detected:  {event.detected_at.isoformat()}",
    ]
    if event.confidence < 1.0:
        lines.append(f"confidence: {event.confidence:.2f}")
    return "\n".join(lines) + "\n"
