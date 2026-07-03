"""Source base class."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ..event import DropEvent
from ..state import State


@dataclass
class SourceContext:
    """Passed to every source at fetch time.

    Kept as a dataclass (not just kwargs) so it's easy to add more knobs later
    without touching every source implementation.
    """

    state: State
    """Shared mutable state; sources may read/write their own scratch here."""

    user_agent: str
    """Polite User-Agent every source should send. Reddit rejects empty UAs."""

    timeout_s: float = 20.0


class Source(abc.ABC):
    """A pluggable signal source."""

    #: Stable id — matches ``id`` in sources.yaml. Used for logging, dedupe,
    #: and cooldown bookkeeping in ``State``.
    id: str

    def __init__(self, source_id: str, options: dict[str, Any]) -> None:
        self.id = source_id
        self.options = options

    @abc.abstractmethod
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        """Return zero or more events. Must not raise for expected failures
        (network hiccups, rate limits) — those should be logged and swallowed
        so one flaky source doesn't take down the whole run."""
