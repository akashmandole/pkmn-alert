"""ntfy.sh notifier — the recommended default.

ntfy is an open-source pub-sub over HTTP. Anyone who knows your topic name
can publish to it, so pick a topic that's essentially a password (e.g.
'pkmn-drops-x7Jq2K'). On your phone, install the ntfy iOS/Android app and
subscribe to that topic. Free, no signup, works on all your devices.

Docs: https://ntfy.sh
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..event import DropEvent
from .base import Notifier

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"


class NtfyNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.server: str = (options.get("server") or DEFAULT_SERVER).rstrip("/")
        self.topic: str = options.get("topic") or ""
        # Access token is only needed for reserved topics on ntfy Pro or
        # self-hosted instances with auth turned on.
        self.token: str = options.get("token") or ""
        self.label = f"ntfy[{self.topic or '<unset>'}]"

    def is_configured(self) -> bool:
        if not self.topic:
            log.debug("%s missing topic; skipping", self.label)
            return False
        return True

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        url = f"{self.server}/{self.topic}"
        headers = {
            "Title": _clip(f"{event.kind.upper()}: {event.retailer}", 100),
            "Priority": _priority_for(event),
            "Tags": _tags_for(event),
        }
        if event.url:
            # Rendered as an actionable button in the ntfy iOS/Android app.
            headers["Actions"] = f"view, Open, {event.url}, clear=true"
            headers["Click"] = event.url
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        body = _format_body(event)
        try:
            resp = httpx.post(url, content=body.encode("utf-8"), headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            log.warning("%s POST failed for subscriber %s: %s", self.label, subscriber_id, exc)
            return False

        if resp.status_code >= 300:
            log.warning(
                "%s POST %s returned HTTP %s: %s",
                self.label, url, resp.status_code, resp.text[:200],
            )
            return False

        log.info("%s -> subscriber=%s ok", self.label, subscriber_id)
        return True


def _priority_for(event: DropEvent) -> str:
    # ntfy priority: 1=min .. 5=max. Queues get max so your phone actually
    # buzzes; deals/news stay at default.
    if event.kind == "queue":
        return "5"
    if event.kind in ("restock", "preorder"):
        return "4"
    return "3"


def _tags_for(event: DropEvent) -> str:
    if event.kind == "queue":
        return "rotating_light,rotating_light,rotating_light"
    if event.kind == "restock":
        return "package"
    if event.kind == "preorder":
        return "calendar"
    if event.kind == "deal":
        return "moneybag"
    return "loudspeaker"


def _format_body(event: DropEvent) -> str:
    lines = [event.title]
    if event.url:
        lines.append("")
        lines.append(event.url)
    lines.append("")
    lines.append(f"source={event.source} retailer={event.retailer} region={event.region}")
    return "\n".join(lines)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
