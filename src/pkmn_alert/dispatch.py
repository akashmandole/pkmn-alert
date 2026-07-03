"""Dispatcher: takes deduped events + subscriber list, sends notifications."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .config import Subscriber
from .event import DropEvent
from .filters import matches
from .notifiers import Notifier, build as build_notifier

log = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    delivered: int = 0
    skipped_filtered: int = 0
    skipped_unconfigured: int = 0
    failed: int = 0


def dispatch(
    events: Iterable[DropEvent],
    subscribers: list[Subscriber],
    now: datetime,
    tz_name: str,
    dry_run: bool = False,
) -> DispatchResult:
    result = DispatchResult()
    events_list = list(events)
    if not events_list:
        return result

    # Build notifier instances once per subscriber, not per event. Notifiers
    # are stateless (they hold config, not connections), so reuse is safe.
    prepared: list[tuple[Subscriber, list[Notifier]]] = []
    for sub in subscribers:
        if not sub.enabled:
            continue
        notifiers: list[Notifier] = []
        for ch in sub.channels:
            n = build_notifier(ch)
            if n is None:
                continue
            if not n.is_configured():
                log.info("subscriber=%s notifier=%s not configured; skipping", sub.id, n.label)
                result.skipped_unconfigured += 1
                continue
            notifiers.append(n)
        if notifiers:
            prepared.append((sub, notifiers))
        else:
            log.info("subscriber=%s has no usable channels; nothing to do", sub.id)

    for event in events_list:
        for sub, notifiers in prepared:
            allowed, reason = matches(event, sub.filters, now, tz_name)
            if not allowed:
                log.debug("subscriber=%s filtered out event %s: %s", sub.id, event.dedupe_key(), reason)
                result.skipped_filtered += 1
                continue

            for n in notifiers:
                if dry_run:
                    log.info(
                        "[dry-run] would send to subscriber=%s via %s: %s",
                        sub.id, n.label, event.title,
                    )
                    result.delivered += 1
                    continue

                ok = n.send(event, sub.id)
                if ok:
                    result.delivered += 1
                else:
                    result.failed += 1

    return result
