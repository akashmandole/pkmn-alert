"""Persistent state — the dedupe cache and per-source cooldowns.

Format is a single JSON file on disk. In the GitHub Actions deployment the
workflow commits this file back to the repo after every run, so the next
run starts with the same dedupe memory. When running locally, ``state.json``
just sits next to the code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# How long a dedupe entry is remembered. 24h means the same drop title won't
# re-alert for a full day, which is longer than any real drop window.
DEDUPE_TTL = timedelta(hours=24)

# Absolute cap on entries so state.json never bloats indefinitely.
MAX_ENTRIES = 500


@dataclass
class State:
    """In-memory view of ``state.json``."""

    # dedupe key -> ISO8601 timestamp when first seen
    seen: dict[str, str] = field(default_factory=dict)
    # source_id -> ISO8601 timestamp; if now < cooldown_until we skip that source
    cooldowns: dict[str, str] = field(default_factory=dict)
    # arbitrary per-source scratch (e.g. Reddit last-seen post id) so sources
    # can dedupe on the source side too and avoid re-fetching identical items.
    source_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Follow-up pushes scheduled for future ticks.
    #   Each entry is a dict with keys:
    #     - event:        DropEvent.to_dict()   — replayed at fire time
    #     - subscriber_id: str
    #     - channel:      {"type": ..., "options": {...}}
    #     - due_iso:      ISO8601 timestamp when this reminder becomes due
    #     - attempt:      int (1 = first reminder after original, ...)
    # We store the fully-hydrated channel dict rather than a channel index
    # so that mid-flight subscriber edits don't break in-flight reminders.
    pending_reminders: list[dict[str, Any]] = field(default_factory=list)
    # Cross-source correlation record: "<retailer>_<kind>" -> ISO8601 timestamp
    # of the most recent direct-observation confirmation. The queueit source
    # writes here; the dispatcher reads here to upgrade Reddit-only events
    # to HIGH confidence when a recent confirmation exists.
    recent_confirmations: dict[str, str] = field(default_factory=dict)
    # Rolling log of "a derivative source produced a fresh, drop-shaped event
    # about the retailer we care about". Feeds the confidence gate on the
    # queueit source so we don't hammer pokemoncenter.com on quiet ticks.
    # Each entry: {"source_id", "kind", "retailer", "at" (ISO8601)}. Pruned
    # to a 60-min window in prune().
    signal_log: list[dict[str, Any]] = field(default_factory=list)
    # When a queueit probe confirms an active queue we open a "burst" window
    # during which the gate is bypassed and we probe every tick — useful for
    # multi-wave drops or catching the queue re-opening. ISO8601 timestamp
    # or empty string when no burst is active.
    probe_burst_until: str = ""

    def has_seen(self, key: str) -> bool:
        return key in self.seen

    def mark_seen(self, key: str, now: datetime) -> None:
        self.seen[key] = now.astimezone(timezone.utc).isoformat()

    def is_in_cooldown(self, source_id: str, now: datetime) -> bool:
        raw = self.cooldowns.get(source_id)
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return False
        return now < until

    def set_cooldown(self, source_id: str, until: datetime) -> None:
        self.cooldowns[source_id] = until.astimezone(timezone.utc).isoformat()

    def record_confirmation(self, retailer: str, kind: str, when: datetime) -> None:
        """Called by direct-observation sources (queueit) to mark that we
        physically saw the drop happen. The dispatcher uses this to upgrade
        derivative sources (Reddit) from MED to HIGH confidence."""
        self.recent_confirmations[f"{retailer}_{kind}"] = (
            when.astimezone(timezone.utc).isoformat()
        )

    def was_recently_confirmed(
        self, retailer: str, kind: str, within: timedelta, now: datetime
    ) -> bool:
        raw = self.recent_confirmations.get(f"{retailer}_{kind}")
        if not raw:
            return False
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            return False
        return (now - when) <= within

    def record_signal(
        self, source_id: str, kind: str, retailer: str, when: datetime,
    ) -> None:
        """Log a fresh drop-shaped event from a derivative source.

        Called by the main loop right after per-source dedupe so the log
        is populated BEFORE queueit's gate check runs (assuming source
        ordering puts queueit last, which __main__ enforces)."""
        self.signal_log.append({
            "source_id": source_id,
            "kind": kind,
            "retailer": retailer,
            "at": when.astimezone(timezone.utc).isoformat(),
        })

    def distinct_signal_sources_since(
        self,
        since: datetime,
        now: datetime,
        exclude: set[str] | None = None,
        kinds: set[str] | None = None,
        retailers: set[str] | None = None,
    ) -> set[str]:
        """Return the set of source_ids that have logged qualifying signals
        within [since, now]. Optional ``kinds`` / ``retailers`` filters
        further restrict what counts as a signal for the caller's purposes."""
        _ = now  # explicit that "now" is here for symmetry / future use
        exclude = exclude or set()
        result: set[str] = set()
        for entry in self.signal_log:
            src = entry.get("source_id", "")
            if src in exclude:
                continue
            if kinds and entry.get("kind") not in kinds:
                continue
            if retailers and entry.get("retailer") not in retailers:
                continue
            raw = entry.get("at", "")
            try:
                at = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if at < since:
                continue
            result.add(src)
        return result

    def is_in_probe_burst(self, now: datetime) -> bool:
        if not self.probe_burst_until:
            return False
        try:
            until = datetime.fromisoformat(self.probe_burst_until)
        except ValueError:
            return False
        return now < until

    def set_probe_burst(self, until: datetime) -> None:
        self.probe_burst_until = until.astimezone(timezone.utc).isoformat()

    def prune(self, now: datetime) -> None:
        cutoff = now - DEDUPE_TTL
        expired = [
            k
            for k, ts in self.seen.items()
            if _parse_iso(ts) < cutoff
        ]
        for k in expired:
            del self.seen[k]

        if len(self.seen) > MAX_ENTRIES:
            # Keep the newest MAX_ENTRIES; ordered by timestamp.
            ordered = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)
            self.seen = dict(ordered[:MAX_ENTRIES])

        # Drop expired cooldowns too.
        self.cooldowns = {
            k: v for k, v in self.cooldowns.items() if _parse_iso(v) > now
        }

        # Prune stale confirmations after 24h — they've long served their
        # purpose of correlating with the same-day Reddit post.
        confirmation_cutoff = now - timedelta(hours=24)
        self.recent_confirmations = {
            k: v
            for k, v in self.recent_confirmations.items()
            if _parse_iso(v) > confirmation_cutoff
        }

        # Drop reminders whose due time is more than 1 hour past. Firing a
        # 3-hour-old reminder is worse than not firing at all.
        reminder_cutoff = now - timedelta(hours=1)
        self.pending_reminders = [
            r for r in self.pending_reminders
            if _parse_iso(r.get("due_iso", "")) > reminder_cutoff
        ]

        # Signal log entries older than 60 min can't influence any gate
        # decision (widest configured window is ~20 min).
        signal_cutoff = now - timedelta(minutes=60)
        self.signal_log = [
            entry for entry in self.signal_log
            if _parse_iso(entry.get("at", "")) > signal_cutoff
        ]

        # Clear expired burst window.
        if self.probe_burst_until and not self.is_in_probe_burst(now):
            self.probe_burst_until = ""


def _parse_iso(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def load(path: Path) -> State:
    if not path.exists():
        log.debug("state file %s does not exist yet, starting fresh", path)
        return State()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("failed to load state from %s (%s); starting fresh", path, exc)
        return State()

    return State(
        seen=dict(raw.get("seen", {})),
        cooldowns=dict(raw.get("cooldowns", {})),
        source_meta=dict(raw.get("source_meta", {})),
        pending_reminders=list(raw.get("pending_reminders", [])),
        recent_confirmations=dict(raw.get("recent_confirmations", {})),
        signal_log=list(raw.get("signal_log", [])),
        probe_burst_until=str(raw.get("probe_burst_until", "") or ""),
    )


def save(path: Path, state: State) -> None:
    payload = {
        "seen": state.seen,
        "cooldowns": state.cooldowns,
        "source_meta": state.source_meta,
        "pending_reminders": state.pending_reminders,
        "recent_confirmations": state.recent_confirmations,
        "signal_log": state.signal_log,
        "probe_burst_until": state.probe_burst_until,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so diffs are stable — important because the GitHub Actions
    # workflow commits this file and we don't want spurious churn.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
