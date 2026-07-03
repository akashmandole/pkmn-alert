"""Per-subscriber filtering (region, retailer, kind, keywords, quiet hours)."""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .config import SubscriberFilters

from .event import DropEvent

log = logging.getLogger(__name__)


def matches(event: DropEvent, f: "SubscriberFilters", now: datetime, tz_name: str) -> tuple[bool, str]:
    """Return (allowed, reason). ``reason`` is a short debug string, empty on
    success. We return a reason so ``--dry-run`` can explain WHY an event
    was skipped for a given subscriber, which is really useful during setup."""

    if event.confidence < f.min_confidence:
        return False, f"confidence {event.confidence:.2f} < {f.min_confidence:.2f}"

    if f.regions and event.region not in f.regions:
        return False, f"region {event.region!r} not in {f.regions}"

    if f.retailers and event.retailer not in f.retailers:
        return False, f"retailer {event.retailer!r} not in {f.retailers}"

    if f.kinds and event.kind not in f.kinds:
        return False, f"kind {event.kind!r} not in {f.kinds}"

    if f.keywords:
        haystack = event.title.lower()
        if not any(k.lower() in haystack for k in f.keywords):
            return False, f"none of keywords {f.keywords} matched title"

    if f.quiet_hours:
        if _in_quiet_hours(f.quiet_hours, now, tz_name):
            # Never mute a live queue — that's the whole point of the tool.
            if event.kind != "queue":
                return False, f"in quiet hours {f.quiet_hours!r}"

    return True, ""


def _in_quiet_hours(spec: str, now: datetime, tz_name: str) -> bool:
    """Parse ``"HH:MM-HH:MM"`` (24h, local to ``tz_name``) and test membership.

    Handles the overnight case (e.g. ``22:00-07:00``). Returns False on
    unparseable specs so a typo doesn't silently swallow every alert."""
    try:
        start_s, end_s = spec.split("-", 1)
        start = _parse_hhmm(start_s.strip())
        end = _parse_hhmm(end_s.strip())
    except (ValueError, AttributeError):
        log.warning("could not parse quiet_hours %r; ignoring", spec)
        return False

    try:
        local_now = now.astimezone(ZoneInfo(tz_name)).time()
    except Exception:
        local_now = now.astimezone(timezone.utc).time()

    if start <= end:
        return start <= local_now < end
    # Wraps midnight.
    return local_now >= start or local_now < end


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":", 1)
    return dtime(hour=int(hh), minute=int(mm))
