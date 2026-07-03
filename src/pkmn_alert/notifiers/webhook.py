"""Generic HTTP webhook notifier.

Pairs perfectly with iOS Shortcuts:
  1. On iPhone, open Shortcuts → create a Personal Automation.
  2. Trigger: "When webhook received" (or add a Shortcut that accepts input
     and expose it via ``https://<icloud>/shortcuts/...``).
  3. Action: Show Notification / Play Sound / Speak Text with the JSON body.
  4. Copy the URL into subscribers.yaml under ``type: webhook``.

Also works with Zapier, Make, n8n, Slack incoming webhooks, Home Assistant,
etc. We POST JSON by default; set ``method: GET`` for query-string flavors.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..event import DropEvent
from .base import Notifier

log = logging.getLogger(__name__)


class WebhookNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.url: str = options.get("url") or ""
        self.method: str = str(options.get("method", "POST")).upper()
        self.extra_headers: dict[str, str] = dict(options.get("headers", {}))
        # If set, the event dict is placed under this key: ``{"event": {...}}``.
        # Handy for platforms that expect a specific envelope.
        self.envelope_key: str = options.get("envelope_key") or ""
        self.label = f"webhook[{self.url or '<unset>'}]"

    def is_configured(self) -> bool:
        if not self.url:
            log.debug("%s missing url; skipping", self.label)
            return False
        return True

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        payload = event.to_dict()
        payload["subscriber_id"] = subscriber_id
        if self.envelope_key:
            payload = {self.envelope_key: payload}

        try:
            if self.method == "GET":
                resp = httpx.get(
                    self.url,
                    params={k: str(v) for k, v in payload.items() if not isinstance(v, dict)},
                    headers=self.extra_headers or None,
                    timeout=15.0,
                    follow_redirects=True,
                )
            else:
                resp = httpx.request(
                    self.method,
                    self.url,
                    json=payload,
                    headers=self.extra_headers or None,
                    timeout=15.0,
                    follow_redirects=True,
                )
        except httpx.HTTPError as exc:
            log.warning("%s request failed for subscriber %s: %s", self.label, subscriber_id, exc)
            return False

        if resp.status_code >= 300:
            log.warning(
                "%s returned HTTP %s: %s",
                self.label, resp.status_code, resp.text[:200],
            )
            return False

        log.info("%s -> subscriber=%s ok", self.label, subscriber_id)
        return True
