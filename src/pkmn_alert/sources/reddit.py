"""Reddit source — polls the public Atom feed for one or more subreddits.

## Why RSS and not the JSON API

In late 2023 Reddit tightened access to their public JSON endpoints
(``/r/xxx/.json``): unauthenticated requests now uniformly return 403,
regardless of User-Agent. The public Atom feed at ``/r/xxx/new.rss`` is
still open — Reddit intends it to be consumed by feed readers, no OAuth
required — and is what every free monitor uses in 2026.

Rate limits are per-UA-per-IP. A descriptive, unique User-Agent (Reddit's
recommended format is ``<platform>:<app-id>:<version> (by /u/<name>)`` but
any distinct string works) drops us into the polite bucket rather than
the anonymous-browser bucket, which gets 429'd almost immediately.

## Multi-sub combined feed

Reddit supports ``/r/a+b+c/new.rss`` which returns the newest posts across
all named subreddits in a SINGLE HTTP request. This is a huge win vs. the
old per-sub loop:

  * one request => one rate-limit hit (previously N requests => cloud-ASN
    per-second throttle would 429 the 2nd+ request even with 6s spacing).
  * ~4x more subs covered for the same rate-limit budget.
  * simpler code — no ordering / partial-failure gymnastics.

## Signal quality

r/PokemonRestocks and r/PKMNTCGDeals are restock-focused. r/PokeInvesting
and r/pokemongoing are broader but often surface drops from a different
angle (invest hype spikes, casual collector chatter). Combined feed means
we catch signal from all four without paying 4x request cost.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx
from dateutil import parser as dateparser

from ..event import DropEvent
from .base import Source, SourceContext

log = logging.getLogger(__name__)

DEFAULT_SUBREDDITS = [
    "PokemonRestocks",
    "PKMNTCGDeals",
    "PokeInvesting",
    "pokemongoing",
]
DEFAULT_LIMIT = 25

# Cooldown when Reddit sends us a 429 or 403. Both signal "back off on this
# UA/IP for a while"; 15 min is enough to clear their sliding window.
RATE_LIMIT_COOLDOWN = timedelta(minutes=15)

# Reddit strips HTML tags from feed entries; we keep this regex around to
# defensively clean any residual tags before matching keywords.
_TAG_RE = re.compile(r"<[^>]+>")


class RedditSource(Source):
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        subs: list[str] = self.options.get("subreddits") or DEFAULT_SUBREDDITS
        limit: int = int(self.options.get("limit", DEFAULT_LIMIT))
        # No default keyword_hints: the configured subs are restock-focused,
        # so we let every post through and rely on per-subscriber
        # `filters.keywords` for the final cut.
        keyword_hint: list[str] = [
            k.lower() for k in self.options.get("keyword_hints", [])
        ]

        if not subs:
            log.warning("[%s] no subreddits configured; nothing to fetch", self.id)
            return []

        # Reddit's built-in multi-sub feed: /r/a+b+c/new.rss returns the
        # newest posts across all named subs in ONE request. Single-sub
        # setups collapse cleanly to /r/a/new.rss.
        combined = "+".join(subs)
        url = f"https://www.reddit.com/r/{combined}/new.rss?limit={limit}"

        resp = self._get(url, ctx)
        if resp is None:
            return []

        if resp.status_code in (429, 403):
            log.warning(
                "[%s] reddit returned HTTP %s on combined feed; cooling down %s",
                self.id, resp.status_code, RATE_LIMIT_COOLDOWN,
            )
            now = datetime.now(tz=timezone.utc)
            ctx.state.set_cooldown(self.id, now + RATE_LIMIT_COOLDOWN)
            return []

        if resp.status_code != 200:
            log.warning(
                "[%s] reddit returned HTTP %s on combined feed",
                self.id, resp.status_code,
            )
            return []

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning(
                "[%s] reddit combined feed parse failed: %s",
                self.id, parsed.bozo_exception,
            )
            return []

        # State-side dedupe against the most-recently-seen entry across the
        # combined feed. This is best-effort — the main pipeline's per-event
        # dedupe (state.seen) is the source of truth. This just lets us bail
        # out of the loop early once we hit a known entry, saving a few
        # microseconds of parsing on quiet ticks.
        meta = ctx.state.source_meta.setdefault(self.id, {})
        last_seen_id = meta.get("last_id_combined")
        first_new_id: str | None = None

        events: list[DropEvent] = []
        for entry in parsed.entries:
            entry_id: str = getattr(entry, "id", "") or ""
            if entry_id and entry_id == last_seen_id:
                break
            if first_new_id is None and entry_id:
                first_new_id = entry_id

            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue

            if keyword_hint and not any(k in title.lower() for k in keyword_hint):
                continue

            link = getattr(entry, "link", "") or ""
            # Best-effort recover which sub this came from (Reddit stamps
            # the entry.tags with subreddit info in the combined feed).
            sub_name = _extract_subreddit(entry) or ""
            events.append(
                DropEvent(
                    source=self.id,
                    title=title,
                    url=link,
                    detected_at=_entry_time(entry),
                    kind=_infer_kind(title),
                    region="US",
                    retailer=_infer_retailer(title),
                    raw={"subreddit": sub_name, "id": entry_id},
                )
            )

        if first_new_id:
            meta["last_id_combined"] = first_new_id

        return events

    def _get(self, url: str, ctx: SourceContext) -> httpx.Response | None:
        try:
            return httpx.get(
                url,
                headers={
                    # Reddit-recommended format. Any unique descriptive string
                    # works; do NOT masquerade as a browser (that gets 429'd
                    # almost immediately since Reddit expects real browsers
                    # to be logged in).
                    "User-Agent": ctx.user_agent,
                    "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=ctx.timeout_s,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("[%s] reddit fetch failed for %s: %s", self.id, url, exc)
            return None


def _extract_subreddit(entry: Any) -> str | None:
    """Best-effort pull the subreddit name out of a combined-feed entry.

    Reddit's Atom entries carry a ``<category term="r/foo"/>`` tag; feedparser
    exposes it as ``entry.tags[i].term``. Returns just the name (no ``r/``
    prefix) or None if the shape doesn't match."""
    tags = getattr(entry, "tags", None) or []
    for tag in tags:
        term = getattr(tag, "term", "") or ""
        if term.startswith("r/"):
            return term[2:]
    # Fallback: parse from the entry link, e.g. https://www.reddit.com/r/foo/comments/...
    link = getattr(entry, "link", "") or ""
    m = re.search(r"reddit\.com/r/([^/]+)/", link)
    return m.group(1) if m else None


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
    if "queue" in t or "waiting room" in t or "waitingroom" in t:
        return "queue"
    if "preorder" in t or "pre-order" in t or "pre order" in t:
        return "preorder"
    if "restock" in t or "back in stock" in t or "live" in t or "dropped" in t or "drop" in t:
        return "restock"
    if "deal" in t or "% off" in t or "discount" in t or "sale" in t:
        return "deal"
    return "news"


def _infer_retailer(title: str) -> str:
    t = _TAG_RE.sub("", title).lower()
    if "pokemon center" in t or "pokémon center" in t or "pokecenter" in t:
        return "pokemoncenter"
    if "target" in t:
        return "target"
    if "walmart" in t:
        return "walmart"
    if "best buy" in t or "bestbuy" in t:
        return "bestbuy"
    if "costco" in t:
        return "costco"
    if "amazon" in t:
        return "amazon"
    if "gamestop" in t:
        return "gamestop"
    return "unknown"
