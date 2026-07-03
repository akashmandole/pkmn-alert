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

## Signal quality

r/PokemonRestocks + r/PKMNTCGDeals are both extremely active during drops.
Users typically post within seconds of a queue going live, and our 5-min
cron cadence gives us plenty of head-room.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx
from dateutil import parser as dateparser

from ..event import DropEvent
from .base import Source, SourceContext

log = logging.getLogger(__name__)

DEFAULT_SUBREDDITS = ["PokemonRestocks", "PKMNTCGDeals"]
DEFAULT_LIMIT = 25

# Cooldown when Reddit sends us a 429 or 403. Both signal "back off on this
# UA/IP for a while"; 15 min is enough to clear their sliding window.
RATE_LIMIT_COOLDOWN = timedelta(minutes=15)

# Reddit's anonymous rate limit is ~10 req/min, but cloud-provider IPs
# (GitHub Actions, AWS, etc.) hit a stricter per-ASN throttle. Empirically
# two back-to-back requests get the second one 429'd. Sleeping 6s between
# subreddits keeps us under the limit while still fitting comfortably in
# our 5-min cron window.
INTER_SUB_DELAY_S = 6.0

# Reddit strips HTML tags from feed entries; we keep this regex around to
# defensively clean any residual tags before matching keywords.
_TAG_RE = re.compile(r"<[^>]+>")


class RedditSource(Source):
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        subs: list[str] = self.options.get("subreddits") or DEFAULT_SUBREDDITS
        limit: int = int(self.options.get("limit", DEFAULT_LIMIT))
        # No default keyword_hints: r/PokemonRestocks and r/PKMNTCGDeals are
        # already restock-only subs, so we let every post through and rely
        # on per-subscriber `filters.keywords` for the final cut.
        keyword_hint: list[str] = [k.lower() for k in self.options.get("keyword_hints", [])]

        # Tests / callers with a single subreddit never actually sleep
        # (delay only fires between subs). Exposed as an option so tests
        # can explicitly zero it out and so users can tune it if their
        # ASN reputation is different from GitHub Actions'.
        delay_s: float = float(self.options.get("inter_sub_delay_s", INTER_SUB_DELAY_S))

        meta = ctx.state.source_meta.setdefault(self.id, {})
        events: list[DropEvent] = []

        for i, sub in enumerate(subs):
            if i > 0 and delay_s > 0:
                # Space requests to stay under Reddit's per-ASN throttle.
                # See INTER_SUB_DELAY_S for rationale.
                time.sleep(delay_s)

            url = f"https://www.reddit.com/r/{sub}/new.rss?limit={limit}"
            resp = self._get(url, ctx)
            if resp is None:
                continue

            if resp.status_code == 429 or resp.status_code == 403:
                # One sub getting rate-limited should NOT freeze the whole
                # source — earlier subs may have succeeded and later ticks
                # might see this sub recover. Skip just this sub and return
                # whatever we already gathered.
                log.warning(
                    "[%s] reddit r/%s returned HTTP %s; skipping this sub for this tick",
                    self.id, sub, resp.status_code,
                )
                # Only fall back to a full-source cooldown when EVERY sub
                # in this run failed and we have nothing to show for it.
                is_last_sub = i == len(subs) - 1
                if is_last_sub and not events:
                    log.warning(
                        "[%s] all subs failed with no events; cooling source down %s",
                        self.id, RATE_LIMIT_COOLDOWN,
                    )
                    now = datetime.now(tz=timezone.utc)
                    ctx.state.set_cooldown(self.id, now + RATE_LIMIT_COOLDOWN)
                continue

            if resp.status_code != 200:
                log.warning("[%s] reddit r/%s returned HTTP %s", self.id, sub, resp.status_code)
                continue

            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                log.warning(
                    "[%s] reddit r/%s feed parse failed: %s",
                    self.id, sub, parsed.bozo_exception,
                )
                continue

            last_id_key = f"last_id_{sub}"
            last_seen_id = meta.get(last_id_key)
            first_new_id: str | None = None

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
                events.append(
                    DropEvent(
                        source=self.id,
                        title=title,
                        url=link,
                        detected_at=_entry_time(entry),
                        kind=_infer_kind(title),
                        region="US",
                        retailer=_infer_retailer(title),
                        raw={"subreddit": sub, "id": entry_id},
                    )
                )

            if first_new_id:
                meta[last_id_key] = first_new_id

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
