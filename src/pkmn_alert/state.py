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
    )


def save(path: Path, state: State) -> None:
    payload = {
        "seen": state.seen,
        "cooldowns": state.cooldowns,
        "source_meta": state.source_meta,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so diffs are stable — important because the GitHub Actions
    # workflow commits this file and we don't want spurious churn.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
