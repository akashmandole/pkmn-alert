"""Nitter source — Twitter/X accounts via public Nitter mirrors.

Twitter's own API is paid; Nitter is a free re-hoster that exposes each
account's timeline as RSS. Public Nitter instances come and go, so we try
a list of mirrors in order and fall back on the first one that responds.

This source is deliberately best-effort — if all mirrors are down we just
return no events and log a warning. The Reddit source is our primary signal.
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

# Ordered by observed reliability as of mid-2026. Public Nitter is a
# hostile-environment problem — instances constantly rate-limit or die.
# xcancel.com is the community's most consistently-up mirror; it blocks
# browser-like User-Agents to discourage scraping, but our default
# descriptive UA sails right through. Everything below xcancel is fallback.
DEFAULT_MIRRORS = [
    "https://xcancel.com",
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.tiekoetter.com",
    "https://nitter.space",
]


class NitterSource(Source):
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        handles: list[str] = self.options.get("handles") or []
        if not handles:
            log.debug("[%s] nitter source has no handles configured", self.id)
            return []

        mirrors: list[str] = self.options.get("mirrors") or DEFAULT_MIRRORS
        keyword_hint: list[str] = [
            k.lower() for k in self.options.get("keyword_hints", ["pokemon center", "queue", "restock", "drop"])
        ]

        events: list[DropEvent] = []
        for handle in handles:
            handle_stripped = handle.lstrip("@")
            parsed = self._fetch_one(handle_stripped, mirrors, ctx)
            if parsed is None:
                # Every mirror failed for this handle. Move on quietly; we
                # already logged inside _fetch_one.
                continue

            for entry in parsed.entries:
                title = (getattr(entry, "title", "") or "").strip()
                if not title:
                    continue
                if keyword_hint and not any(k in title.lower() for k in keyword_hint):
                    continue

                events.append(
                    DropEvent(
                        source=self.id,
                        title=f"@{handle_stripped}: {title}",
                        url=_canonical_x_url(handle_stripped, getattr(entry, "link", "")),
                        detected_at=_entry_time(entry),
                        kind=_infer_kind(title),
                        region="US",
                        retailer="pokemoncenter"
                        if "pokemon center" in title.lower() or "pokémon center" in title.lower()
                        else "unknown",
                        raw={"handle": handle_stripped},
                    )
                )
        return events

    def _fetch_one(
        self,
        handle: str,
        mirrors: list[str],
        ctx: SourceContext,
    ) -> Any:
        for mirror in mirrors:
            url = f"{mirror.rstrip('/')}/{handle}/rss"
            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": ctx.user_agent, "Accept": "application/rss+xml, application/xml"},
                    timeout=ctx.timeout_s,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                log.debug("[%s] nitter mirror %s failed for @%s: %s", self.id, mirror, handle, exc)
                continue

            if resp.status_code != 200 or not resp.content:
                log.debug(
                    "[%s] nitter mirror %s returned HTTP %s for @%s",
                    self.id, mirror, resp.status_code, handle,
                )
                continue

            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                log.debug(
                    "[%s] nitter mirror %s parse failed for @%s: %s",
                    self.id, mirror, handle, parsed.bozo_exception,
                )
                continue
            return parsed

        log.warning("[%s] all nitter mirrors failed for @%s", self.id, handle)
        return None


def _canonical_x_url(handle: str, nitter_link: str) -> str:
    # Nitter links are like https://nitter.xyz/user/status/1234; rewrite to
    # x.com so the URL survives the mirror going away.
    if not nitter_link:
        return f"https://x.com/{handle}"
    idx = nitter_link.find("/status/")
    if idx == -1:
        return f"https://x.com/{handle}"
    tail = nitter_link[idx:]
    return f"https://x.com/{handle}{tail}"


def _entry_time(entry: Any) -> datetime:
    for key in ("published", "updated"):
        raw = getattr(entry, key, None)
        if raw:
            try:
                return dateparser.parse(raw).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
    return datetime.now(tz=timezone.utc)


def _infer_kind(title: str) -> str:
    t = title.lower()
    if "queue" in t or "waiting room" in t:
        return "queue"
    if "preorder" in t or "pre-order" in t:
        return "preorder"
    if "live" in t or "restock" in t or "dropped" in t or "in stock" in t:
        return "restock"
    return "news"
