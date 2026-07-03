"""Dispatcher: turns deduped events into notifications.

## Confidence labeling (HIGH / MED)

Every push carries a confidence tag prefixed to the title:

  ``[HIGH]``  We have a direct, physical confirmation of the drop — the
              queueit source saw pokemoncenter.com in queue mode. Reddit
              also fired. Highest possible signal.

  ``[MED]``   Reddit posted about a drop, but our direct probe hasn't
              (yet) seen the queue. Could be a slow Reddit post about
              a real event we're about to catch, or could be someone
              posting stale/wrong info. Still worth waking you up for
              but flagged so you know it's derivative-only.

The ``queueit`` source is treated as a **confirmation-only** signal:
its events are absorbed into state's ``recent_confirmations`` table
but never dispatched on their own. This matches the user's explicit
requirement that queueit "only be used for confirmation" of Reddit
signals, not as a standalone alert source.

## Reminders

Each dispatched event optionally schedules follow-up pushes based on
each subscriber's ``reminders_minutes`` list. Reminders live in
``state.pending_reminders`` and are fired by ``process_due_reminders()``
at the start of every cron tick BEFORE the next fetch. When a reminder
fires, its confidence label is re-computed from current state — so a
MED alert can naturally become a HIGH reminder if the queueit source
confirmed it in the interim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import ChannelConfig, Subscriber
from .event import DropEvent
from .filters import matches
from .notifiers import Notifier, build as build_notifier
from .state import State

log = logging.getLogger(__name__)


# How long a queueit confirmation stays "valid" for upgrading a Reddit
# event to HIGH. A queue that went live 30 min ago and then closed is
# still convincing evidence that a real drop happened.
CONFIRMATION_LOOKBACK = timedelta(minutes=30)

# Reminder attempts older than this cutoff are quietly dropped rather
# than fired late (see State.prune()).
REMINDER_MAX_LATENESS = timedelta(minutes=15)


@dataclass
class DispatchResult:
    delivered: int = 0
    reminders_fired: int = 0
    reminders_queued: int = 0
    confirmations_recorded: int = 0
    skipped_filtered: int = 0
    skipped_unconfigured: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def process_due_reminders(
    state: State,
    subscribers: list[Subscriber],
    now: datetime,
    tz_name: str,
    dry_run: bool = False,
) -> int:
    """Fire any pending reminders whose due time has arrived. Returns count fired.

    Called at the START of every cron tick so reminders don't get
    starved by long-running fetches later in the same tick.
    """
    if not state.pending_reminders:
        return 0

    still_pending: list[dict] = []
    fired = 0
    subs_by_id = {s.id: s for s in subscribers if s.enabled}

    for r in state.pending_reminders:
        try:
            due = datetime.fromisoformat(r["due_iso"])
        except (KeyError, ValueError):
            log.warning("dropping reminder with malformed due_iso: %r", r)
            continue

        if now < due:
            still_pending.append(r)
            continue

        # Dropped-too-late: if the reminder is much later than intended,
        # skip it. Prevents "surprise" alerts from a workflow that was
        # paused for hours.
        if now - due > REMINDER_MAX_LATENESS:
            log.info(
                "dropping stale reminder due at %s (now %s): %s",
                r.get("due_iso"), now.isoformat(), r.get("event", {}).get("title"),
            )
            continue

        sub = subs_by_id.get(r.get("subscriber_id"))
        if sub is None:
            log.info(
                "reminder subscriber_id=%r no longer exists; dropping",
                r.get("subscriber_id"),
            )
            continue

        try:
            event = DropEvent.from_dict(r["event"])
        except (KeyError, ValueError) as exc:
            log.warning("dropping malformed reminder event: %s", exc)
            continue

        attempt = int(r.get("attempt", 1))
        # Re-evaluate the confidence label at fire time — the queueit
        # source may have confirmed in the interim.
        label = _label_for(event, state, now)
        display = _prefix_title(event, label=label, reminder_attempt=attempt)

        notifier = _build_notifier_from_snapshot(r.get("channel", {}))
        if notifier is None or not notifier.is_configured():
            log.info(
                "reminder channel %r no longer usable; dropping",
                r.get("channel", {}).get("type"),
            )
            continue

        if dry_run:
            log.info(
                "[dry-run] would fire reminder attempt=%d to %s via %s: %s",
                attempt, sub.id, notifier.label, display.title,
            )
            fired += 1
            continue

        ok = notifier.send(display, sub.id)
        if ok:
            fired += 1
            log.info(
                "reminder fired attempt=%d subscriber=%s label=%s: %s",
                attempt, sub.id, label, event.title[:80],
            )
        else:
            log.warning(
                "reminder send failed attempt=%d subscriber=%s: %s",
                attempt, sub.id, event.title[:80],
            )

    state.pending_reminders = still_pending
    return fired


def dispatch(
    events: Iterable[DropEvent],
    subscribers: list[Subscriber],
    now: datetime,
    tz_name: str,
    state: State | None = None,
    dry_run: bool = False,
) -> DispatchResult:
    """Send notifications for a batch of fresh events.

    ``state`` is optional so existing test fixtures that don't need
    confirmations or reminders keep working. When state is None we run
    in the "dumb" pre-labeling mode used by legacy tests.
    """
    result = DispatchResult()
    events_list = list(events)
    if not events_list:
        return result

    # 1) Absorb queueit events as confirmations. They never dispatch.
    dispatchable: list[DropEvent] = []
    if state is not None:
        for event in events_list:
            if event.source == "queueit" and event.retailer == "pokemoncenter":
                state.record_confirmation(event.retailer, event.kind, now)
                result.confirmations_recorded += 1
                log.info(
                    "recorded confirmation retailer=%s kind=%s at %s",
                    event.retailer, event.kind, now.isoformat(),
                )
                continue
            dispatchable.append(event)
    else:
        dispatchable = events_list

    if not dispatchable:
        return result

    # 2) Prepare per-subscriber notifiers once. Notifiers are stateless
    #    (they hold config, not connections), so reuse is safe within a run.
    prepared: list[tuple[Subscriber, list[tuple[ChannelConfig, Notifier]]]] = []
    for sub in subscribers:
        if not sub.enabled:
            continue
        channels: list[tuple[ChannelConfig, Notifier]] = []
        for ch in sub.channels:
            n = build_notifier(ch)
            if n is None:
                continue
            if not n.is_configured():
                log.info("subscriber=%s notifier=%s not configured; skipping", sub.id, n.label)
                result.skipped_unconfigured += 1
                continue
            channels.append((ch, n))
        if channels:
            prepared.append((sub, channels))
        else:
            log.info("subscriber=%s has no usable channels; nothing to do", sub.id)

    # 3) Dispatch each event to each qualifying subscriber, label + queue reminders.
    for event in dispatchable:
        label = _label_for(event, state, now) if state is not None else "MED"

        for sub, channels in prepared:
            allowed, reason = matches(event, sub.filters, now, tz_name)
            if not allowed:
                log.debug(
                    "subscriber=%s filtered out event %s: %s",
                    sub.id, event.dedupe_key(), reason,
                )
                result.skipped_filtered += 1
                continue

            display = _prefix_title(event, label=label, reminder_attempt=0)

            for cfg, notifier in channels:
                if dry_run:
                    log.info(
                        "[dry-run] would send to subscriber=%s via %s: %s",
                        sub.id, notifier.label, display.title,
                    )
                    result.delivered += 1
                    continue

                ok = notifier.send(display, sub.id)
                if ok:
                    result.delivered += 1
                else:
                    result.failed += 1

                # Queue reminders only after a successful primary send.
                # Sending a "reminder" for a message that never went out
                # would be confusing.
                if ok and state is not None and sub.reminders_minutes:
                    queued = _queue_reminders(
                        state, event, sub, cfg, now,
                    )
                    result.reminders_queued += queued

    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _label_for(event: DropEvent, state: State | None, now: datetime) -> str:
    """Compute the confidence label to prefix onto the notification title.

    - Direct-observation source (queueit) => HIGH  (shouldn't dispatch,
      but if it does via unit tests, the label reflects reality).
    - Any other source that matches a recent queueit confirmation => HIGH.
    - Everything else => MED.
    """
    if event.source == "queueit":
        return "HIGH"
    if state is None:
        return "MED"
    if state.was_recently_confirmed(
        event.retailer, event.kind, CONFIRMATION_LOOKBACK, now
    ):
        return "HIGH"
    return "MED"


def _prefix_title(event: DropEvent, *, label: str, reminder_attempt: int) -> DropEvent:
    """Return a new DropEvent whose title carries the label + optional
    reminder marker. We build a fresh event rather than mutating because
    DropEvent is frozen (and mutation would break dedupe)."""
    if reminder_attempt > 0:
        prefix = f"[{label}][REMIND #{reminder_attempt}] "
    else:
        prefix = f"[{label}] "
    return DropEvent(
        source=event.source,
        title=prefix + event.title,
        url=event.url,
        detected_at=event.detected_at,
        kind=event.kind,
        region=event.region,
        retailer=event.retailer,
        confidence=event.confidence,
        raw=event.raw,
    )


def _queue_reminders(
    state: State,
    event: DropEvent,
    sub: Subscriber,
    channel_cfg: ChannelConfig,
    now: datetime,
) -> int:
    """Append one pending reminder per configured offset. Returns count queued."""
    queued = 0
    for i, delta_min in enumerate(sub.reminders_minutes, start=1):
        due = now + timedelta(minutes=int(delta_min))
        state.pending_reminders.append({
            "event": event.to_dict(),
            "subscriber_id": sub.id,
            "channel": {"type": channel_cfg.type, "options": channel_cfg.options},
            "due_iso": due.astimezone(timezone.utc).isoformat(),
            "attempt": i,
        })
        queued += 1
    return queued


def _build_notifier_from_snapshot(snap: dict) -> Notifier | None:
    """Rebuild a Notifier from a channel dict frozen at reminder-queue time."""
    ch_type = snap.get("type")
    if not ch_type:
        return None
    return build_notifier(ChannelConfig(type=ch_type, options=dict(snap.get("options", {}))))
