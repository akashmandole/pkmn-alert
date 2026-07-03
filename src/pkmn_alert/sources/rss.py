"""Generic RSS/Atom source.

Point it at any RSS/Atom URL and it'll turn each item into a DropEvent.
Useful for community feeds, Discord-webhook-to-RSS bridges, or the RSS
outputs some monitor sites publish.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from dateutil import parser as dateparser

from ..event import DropEvent
from .base import Source, SourceContext

log = logging.getLogger(__name__)


class RSSSource(Source):
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        feed_url: str = self.options.get("url", "")
        if not feed_url:
            log.warning("[%s] rss source has no 'url' configured; skipping", self.id)
            return []

        retailer: str = self.options.get("retailer", "unknown")
        region: str = self.options.get("region", "US")
        default_kind: str = self.options.get("kind", "restock")
        keyword_hint: list[str] = [k.lower() for k in self.options.get("keyword_hints", [])]

        # feedparser is happy to fetch on its own but its User-Agent is
        # generic and gets 403'd by some CDNs. Do the fetch ourselves so we
        # can send the polite UA + support gzip.
        try:
            resp = httpx.get(
                feed_url,
                headers={"User-Agent": ctx.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
                timeout=ctx.timeout_s,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("[%s] rss fetch failed for %s: %s", self.id, feed_url, exc)
            return []

        if resp.status_code != 200:
            log.warning("[%s] rss %s returned HTTP %s", self.id, feed_url, resp.status_code)
            return []

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning("[%s] rss %s failed to parse: %s", self.id, feed_url, parsed.bozo_exception)
            return []

        events: list[DropEvent] = []
        for entry in parsed.entries:
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue
            if keyword_hint and not any(k in title.lower() for k in keyword_hint):
                continue

            detected = _entry_time(entry)
            events.append(
                DropEvent(
                    source=self.id,
                    title=title,
                    url=(getattr(entry, "link", "") or ""),
                    detected_at=detected,
                    kind=default_kind,
                    region=region,
                    retailer=retailer,
                    raw={"feed_url": feed_url},
                )
            )
        return events


def _entry_time(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        raw = getattr(entry, key, None)
        if raw:
            try:
                return dateparser.parse(raw).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
    return datetime.now(tz=timezone.utc)
