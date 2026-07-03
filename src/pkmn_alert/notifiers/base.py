"""Notifier base class."""

from __future__ import annotations

import abc
from typing import Any

from ..event import DropEvent


class Notifier(abc.ABC):
    """A single delivery channel for one subscriber (e.g. one phone, one email)."""

    #: Stable label used in logs; concrete notifiers set this in __init__.
    label: str = "?"

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    @abc.abstractmethod
    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        """Deliver one event. Return True on success, False on any failure.
        Must not raise for expected transport errors — the caller aggregates
        successes/failures for the batch."""

    def is_configured(self) -> bool:
        """Return True if this notifier has everything it needs to actually
        try a send. Notifiers with missing secrets (e.g. empty topic name
        because the env var wasn't set) should return False so the dispatcher
        can skip them with a friendly log message instead of erroring."""
        return True
