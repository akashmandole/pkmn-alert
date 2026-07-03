"""Stdout notifier — for local testing and dry-runs.

Always "configured", always succeeds. Useful as a default channel in the
example config so ``--dry-run`` produces visible output out of the box.
"""

from __future__ import annotations

import logging
from typing import Any

from ..event import DropEvent
from .base import Notifier

log = logging.getLogger(__name__)


class StdoutNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.label = "stdout"

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        # Deliberately use print instead of log so it's obvious in CLI use
        # even when log level is WARN.
        print(
            f"[ALERT to={subscriber_id}] {event.kind.upper()} @ {event.retailer} :: "
            f"{event.title}  {event.url}".rstrip()
        )
        return True
