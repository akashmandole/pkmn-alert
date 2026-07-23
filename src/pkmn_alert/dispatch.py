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

## Confirmation-required subscribers

A subscriber with ``require_confirmation=True`` opts out of MED
entirely — the dispatcher drops any event whose label isn't HIGH
before running filters or building notifiers. The same gate is
re-applied to reminders at fire time. Use this when false positives
are more painful than the occasional missed drop. Skipped events do
NOT mark the drop as alerted, so a subsequent tick that DOES land a
queueit confirmation will still push a fresh HIGH.

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

# When queueit confirms an active queue we open a "probe burst" — the
# queueit gate is bypassed and we hit pokemoncenter.com every 5 min so
# we can catch multi-wave restocks and the queue re-opening. Value must
# stay in sync with sources/queueit.py DEFAULT_BURST_MINUTES.
PROBE_BURST_DURATION = timedelta(minutes=30)

# Once we've fired an alert for a given (retailer, kind) pair, suppress
# any further alerts about the SAME drop for this long. Prevents a real
# drop from producing back-to-back pushes just because Reddit is buzzing
# about it. Reminders scheduled at the moment of the initial alert are
# unaffected — they fire on their own cadence.
#
# Value chosen to comfortably exceed the widest sensible reminders_minutes
# spread ([15, 30]), so the "3 pings per drop" budget is enforced end-to-end:
# initial + reminder@+15 + reminder@+30 = 3, then suppression relaxes.
DROP_DEDUPE_WINDOW = timedelta(minutes=60)


@dataclass
class DispatchResult:
    delivered: int = 0
    reminders_fired: int = 0
    reminders_queued: int = 0
    confirmations_recorded: int = 0
    skipped_filtered: int = 0
    skipped_unconfigured: int = 0
    #: Events collapsed into an earlier event this tick (same retailer+kind).
    coalesced: int = 0
    #: Events suppressed because we already alerted about this drop in a
    #: recent tick and the suppression window hasn't elapsed yet.
    suppressed_recent_drop: int = 0
    #: MED-labeled events dropped because the subscriber has
    #: ``require_confirmation=True`` and no queueit confirmation is on
    #: file for this (retailer, kind). Counted once per (event × subscriber).
    skipped_awaiting_confirmation: int = 0
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

        # False-positive lockdown for reminders. If the subscriber
        # requires confirmation and the reminder is still MED, drop it
        # (don't re-queue). The reasoning: the initial push was gated
        # by the same rule, so if the reminder made it into the queue
        # at all the initial WAS a HIGH; a MED reminder now means the
        # confirmation aged out of CONFIRMATION_LOOKBACK. Sending a
        # MED-labeled follow-up after the confirmation expired is
        # exactly the kind of noise the user asked us to suppress.
        if sub.require_confirmation and label != "HIGH":
            log.info(
                "dropping reminder attempt=%d subscriber=%s: label=%s at fire time "
                "but subscriber requires HIGH-only",
                attempt, sub.id, label,
            )
            continue

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
                # Open a probe burst so the next 30 min of queueit ticks
                # bypass the confidence gate. If we're mid-burst already
                # this just extends it, which is what we want when waves
                # of drops keep arriving.
                state.set_probe_burst(now + PROBE_BURST_DURATION)
                result.confirmations_recorded += 1
                log.info(
                    "recorded confirmation retailer=%s kind=%s at %s; probe burst extended to %s",
                    event.retailer, event.kind, now.isoformat(), state.probe_burst_until,
                )
                continue
            dispatchable.append(event)
    else:
        dispatchable = events_list

    if not dispatchable:
        return result

    # 1.5) Coalesce + cross-tick dedupe.
    #
    # A single real-world drop typically produces N Reddit posts within
    # a few minutes (e.g. "Queue is up!", "Queue is Live!", "PKC Drop at
    # 10:30 AM PST" — all one event). Before this step, N posts meant N
    # back-to-back pushes. After this step:
    #   - WITHIN this tick: events with the same (retailer, kind) collapse
    #     to a single representative event (the earliest by detected_at).
    #     Rest are counted as ``result.coalesced``.
    #   - ACROSS ticks: if an earlier tick already fired an alert for this
    #     (retailer, kind) within DROP_DEDUPE_WINDOW, the whole group is
    #     dropped. Rest are counted as ``result.suppressed_recent_drop``.
    #
    # Runs BEFORE notifier prep so we don't waste time building notifiers
    # for events we're about to discard.
    if state is not None:
        dispatchable, coalesced_ct, suppressed_ct = _coalesce_and_dedupe(
            dispatchable, state, now,
        )
        result.coalesced += coalesced_ct
        result.suppressed_recent_drop += suppressed_ct
        if not dispatchable:
            log.info(
                "no dispatchable events after coalesce+dedupe (coalesced=%d, suppressed=%d)",
                coalesced_ct, suppressed_ct,
            )
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
        any_real_delivery = False

        for sub, channels in prepared:
            # False-positive lockdown. Subscribers with
            # ``require_confirmation`` only want events physically
            # verified on pokemoncenter.com. A MED label means either
            # (a) queueit hasn't confirmed for this (retailer, kind) or
            # (b) it did confirm but the confirmation aged out of
            # CONFIRMATION_LOOKBACK. Either way: silently drop.
            #
            # Order matters: check BEFORE filters/keywords so a MED
            # event that would have passed filters still doesn't
            # increment skipped_filtered.
            if sub.require_confirmation and label != "HIGH":
                log.debug(
                    "subscriber=%s awaiting confirmation; dropping label=%s event %s",
                    sub.id, label, event.dedupe_key(),
                )
                result.skipped_awaiting_confirmation += 1
                continue

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
                    any_real_delivery = True
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

        # Record the drop as alerted only after a real send succeeded for
        # SOME subscriber. On total failure (network hiccup, all filters
        # rejected) we deliberately leave the window open so the next tick
        # gets another shot. Note: dry_run intentionally does NOT mark, so
        # test/preview runs remain side-effect-free w.r.t. suppression.
        if state is not None and any_real_delivery:
            state.mark_drop_alerted(State.drop_key(event.retailer, event.kind), now)

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


def _coalesce_and_dedupe(
    events: list[DropEvent],
    state: State,
    now: datetime,
) -> tuple[list[DropEvent], int, int]:
    """Collapse per-drop duplicates and suppress recently-alerted drops.

    Returns (survivors, coalesced_count, suppressed_count).

    Coalescing (within-tick): events sharing the same
    ``State.drop_key(retailer, kind)`` collapse to one representative,
    picked as the earliest by ``detected_at`` — that's the post whose
    title most likely says "Queue is up" rather than a later reaction
    thread. Ties broken by ``dedupe_key()`` for determinism.

    Suppression (cross-tick): a group whose drop_key was alerted within
    ``DROP_DEDUPE_WINDOW`` is dropped entirely; the earlier alert (and
    its queued reminders) already carry the user."""
    if not events:
        return [], 0, 0

    # Group by drop_key preserving input order.
    groups: dict[str, list[DropEvent]] = {}
    order: list[str] = []
    for ev in events:
        key = State.drop_key(ev.retailer, ev.kind)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ev)

    survivors: list[DropEvent] = []
    coalesced = 0
    suppressed = 0

    for key in order:
        bucket = groups[key]

        if state.was_drop_recently_alerted(key, DROP_DEDUPE_WINDOW, now):
            suppressed += len(bucket)
            titles = ", ".join(e.title[:40] for e in bucket[:3])
            log.info(
                "suppressed %d event(s) for drop_key=%s (alerted within %s): %s%s",
                len(bucket), key, DROP_DEDUPE_WINDOW, titles,
                "..." if len(bucket) > 3 else "",
            )
            continue

        # Coalesce: pick the earliest post as the representative.
        bucket.sort(key=lambda e: (e.detected_at, e.dedupe_key()))
        representative = bucket[0]
        coalesced += len(bucket) - 1
        if len(bucket) > 1:
            log.info(
                "coalesced %d event(s) for drop_key=%s -> %r",
                len(bucket), key, representative.title[:80],
            )
        survivors.append(representative)

    return survivors, coalesced, suppressed
