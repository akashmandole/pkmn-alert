"""Normalized event that every source produces and every notifier consumes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DropEvent:
    """A single potential drop/restock/queue signal.

    Sources normalize whatever they scrape into this shape so the rest of the
    pipeline never has to care where the signal came from.
    """

    source: str
    """Stable id of the source that produced this event (e.g. 'reddit', 'queueit')."""

    title: str
    """Human-readable one-liner shown in the notification."""

    url: str
    """Best-effort clickable URL. Empty string if the source has none."""

    detected_at: datetime
    """When we saw it (UTC)."""

    kind: str = "restock"
    """One of: 'queue', 'restock', 'preorder', 'deal', 'news'."""

    region: str = "US"
    """ISO-ish region code so subscribers can filter."""

    retailer: str = "pokemoncenter"
    """Retailer id. Kept generic so we can extend beyond Pokemon Center later."""

    confidence: float = 1.0
    """0.0–1.0. Currently only the `queueit` source uses <1.0."""

    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    """Optional source-specific payload, kept for debugging."""

    def dedupe_key(self) -> str:
        """Stable key used by the deduper.

        We deliberately collapse whitespace and lowercase the title so that
        two sources posting the same drop with slightly different phrasing
        still collapse to one alert. We DO NOT include ``detected_at`` in the
        key — that would defeat the whole point.
        """
        normalized = _WHITESPACE_RE.sub(" ", self.title.strip().lower())[:120]
        raw = f"{self.retailer}|{self.kind}|{normalized}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "detected_at": self.detected_at.astimezone(timezone.utc).isoformat(),
            "kind": self.kind,
            "region": self.region,
            "retailer": self.retailer,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DropEvent":
        """Rehydrate a DropEvent that was serialized via ``to_dict()``.

        Used by the reminder system, which persists events to state.json
        and replays them a fixed delay later. We intentionally do NOT
        round-trip the ``raw`` field because it can contain arbitrary
        source-specific payloads that would bloat the state file."""
        return cls(
            source=d["source"],
            title=d["title"],
            url=d.get("url", ""),
            detected_at=datetime.fromisoformat(d["detected_at"]),
            kind=d.get("kind", "restock"),
            region=d.get("region", "US"),
            retailer=d.get("retailer", "unknown"),
            confidence=float(d.get("confidence", 1.0)),
        )
