"""Free SMS via carrier email-to-SMS gateways.

Every major US carrier accepts email at ``<10-digit-number>@<gateway>`` and
forwards it to the subscriber's phone as an SMS (or MMS for longer bodies).
This lets us send real text messages without paying Twilio, at the cost of
some delay (usually seconds, occasionally minutes) and no delivery receipts.

## Known gateways (as of 2026)

| Carrier      | SMS gateway              | MMS gateway            |
| ------------ | ------------------------ | ---------------------- |
| AT&T         | txt.att.net              | mms.att.net            |
| T-Mobile     | tmomail.net              | tmomail.net            |
| Verizon      | vtext.com                | vzwpix.com             |
| Sprint       | messaging.sprintpcs.com  | pm.sprint.com          |
| US Cellular  | email.uscc.net           | mms.uscc.net           |
| Boost Mobile | sms.myboostmobile.com    | myboostmobile.com      |
| Cricket      | mms.cricketwireless.net  | mms.cricketwireless.net|
| Xfinity Mob. | vtext.com                | mypixmessages.com      |
| Google Fi    | msg.fi.google.com        | msg.fi.google.com      |

Reliability caveats:
  - Some carriers (Verizon in particular) periodically restrict inbound
    email-to-SMS to combat spam. If yours stops working, switch this
    subscriber's channel to ``ntfy`` or ``webhook``.
  - Bodies are truncated to 140 chars to stay in one SMS segment.
  - The "from" address must be a real address you control (Gmail app
    password works fine); some carriers drop mail from unknown senders.

This notifier internally reuses ``EmailNotifier``, so it inherits all the
Gmail setup instructions — you only supply phone + carrier.
"""

from __future__ import annotations

import logging
from typing import Any

from ..event import DropEvent
from .base import Notifier
from .email_smtp import EmailNotifier

log = logging.getLogger(__name__)

CARRIER_GATEWAYS: dict[str, str] = {
    "att": "txt.att.net",
    "at&t": "txt.att.net",
    "tmobile": "tmomail.net",
    "t-mobile": "tmomail.net",
    "verizon": "vtext.com",
    "sprint": "messaging.sprintpcs.com",
    "uscellular": "email.uscc.net",
    "us cellular": "email.uscc.net",
    "boost": "sms.myboostmobile.com",
    "cricket": "mms.cricketwireless.net",
    "xfinity": "vtext.com",
    "googlefi": "msg.fi.google.com",
    "google fi": "msg.fi.google.com",
}


class SmsGatewayNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        raw_number: str = str(options.get("number") or "")
        self.number = "".join(ch for ch in raw_number if ch.isdigit())
        carrier_raw: str = str(options.get("carrier") or "").strip().lower()
        self.carrier = carrier_raw
        gateway = CARRIER_GATEWAYS.get(carrier_raw)
        # Users can override with an explicit gateway if their carrier
        # isn't in our table.
        gateway = options.get("gateway") or gateway
        self.gateway = gateway or ""

        if self.number and self.gateway:
            recipient = f"{self.number}@{self.gateway}"
        else:
            recipient = ""

        # Delegate the actual SMTP send to EmailNotifier, but force short
        # bodies since anything >140 chars gets split into multiple texts.
        email_opts = {
            "host": options.get("host", "smtp.gmail.com"),
            "port": options.get("port", 587),
            "username": options.get("username"),
            "password": options.get("password"),
            "from": options.get("from") or options.get("username"),
            "to": recipient,
            "starttls": options.get("starttls", True),
        }
        self._email = EmailNotifier(email_opts)
        self.label = f"sms[{self.number or '<unset>'}@{self.gateway or '?'}]"

    def is_configured(self) -> bool:
        if not self.number:
            log.debug("%s missing number; skipping", self.label)
            return False
        if not self.gateway:
            log.warning(
                "%s unknown carrier %r; add 'gateway: <domain>' to override "
                "or switch to ntfy/webhook",
                self.label, self.carrier,
            )
            return False
        return self._email.is_configured()

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        # Compose a compact SMS body. Include a clickable link if we have one
        # and there's room; the carrier will auto-linkify it as MMS otherwise.
        parts = [event.title]
        if event.url:
            parts.append(event.url)
        body = " — ".join(parts)
        if len(body) > 140:
            body = body[:137] + "..."

        shim = _SmsEvent.from_event(event, body)
        return self._email.send(shim, subscriber_id)


class _SmsEvent(DropEvent):
    """Tiny shim: an event whose title is already the SMS-length body.

    We can't mutate a frozen DropEvent, so we build a fresh one that
    EmailNotifier will render with our body as the "title" — good enough
    since the SMS gateway strips subject anyway.
    """

    @classmethod
    def from_event(cls, event: DropEvent, body: str) -> DropEvent:
        return DropEvent(
            source=event.source,
            title=body,
            url="",  # already inlined into title
            detected_at=event.detected_at,
            kind=event.kind,
            region=event.region,
            retailer=event.retailer,
            confidence=event.confidence,
            raw=event.raw,
        )
