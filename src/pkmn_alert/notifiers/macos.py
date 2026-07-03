"""macOS native notification via osascript.

Only fires meaningfully when the process is running on macOS — on Linux
(e.g. inside a GitHub Actions runner) the subprocess call will fail
harmlessly and we return False.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any

from ..event import DropEvent
from .base import Notifier

log = logging.getLogger(__name__)


class MacOSNotifier(Notifier):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.sound: str = options.get("sound") or "Glass"
        self.label = "macos"

    def is_configured(self) -> bool:
        if sys.platform != "darwin":
            log.debug("%s only runs on macOS (current: %s)", self.label, sys.platform)
            return False
        if not shutil.which("osascript"):
            log.debug("%s osascript not found on PATH", self.label)
            return False
        return True

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        title = f"Pokemon {event.kind.upper()}"
        body = event.title.replace('"', "'")
        script = (
            f'display notification "{body}" '
            f'with title "{title}" '
            f'sound name "{self.sound}"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            log.warning("%s osascript failed: %s", self.label, exc)
            return False

        log.info("%s -> subscriber=%s ok", self.label, subscriber_id)
        return True
