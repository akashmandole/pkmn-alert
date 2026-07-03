"""Config loading: sources.yaml + subscribers.yaml (+ env overrides for secrets)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ${ENV_VAR} interpolation in YAML values so secrets live only in env / GH secrets.
_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace ``${FOO}`` in string values with ``os.environ['FOO']``.

    Missing env vars resolve to an empty string. Callers are responsible for
    treating an empty required field as "not configured" and skipping.
    """
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return _interpolate(raw)


@dataclass
class SourceConfig:
    id: str
    type: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelConfig:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubscriberFilters:
    regions: list[str] = field(default_factory=list)
    retailers: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    min_confidence: float = 0.0
    max_per_day: int = 20
    quiet_hours: str | None = None  # e.g. "23:00-07:00" in America/Los_Angeles


@dataclass
class Subscriber:
    id: str
    enabled: bool
    channels: list[ChannelConfig]
    filters: SubscriberFilters
    #: Minutes-after-the-original-push at which to send follow-up reminder
    #: notifications. Empty list = no reminders (one-and-done). Example:
    #: ``[15]`` sends one reminder 15 minutes after the first alert;
    #: ``[10, 20]`` sends two reminders at +10 min and +20 min.
    #:
    #: Reminders re-evaluate their confidence label at fire time — if the
    #: queueit source has since confirmed the drop, a MED alert can be
    #: upgraded to HIGH by the time its reminder goes out.
    reminders_minutes: list[int] = field(default_factory=list)


@dataclass
class AppConfig:
    sources: list[SourceConfig]
    subscribers: list[Subscriber]
    state_path: Path
    timezone: str = "America/Los_Angeles"


def load(
    sources_path: Path,
    subscribers_path: Path,
    state_path: Path,
    timezone_name: str = "America/Los_Angeles",
) -> AppConfig:
    sources_raw = _load_yaml(sources_path)
    subscribers_raw = _load_yaml(subscribers_path)

    sources: list[SourceConfig] = []
    for entry in sources_raw.get("sources", []):
        sources.append(
            SourceConfig(
                id=entry["id"],
                type=entry["type"],
                enabled=bool(entry.get("enabled", True)),
                options=dict(entry.get("options", {})),
            )
        )

    subscribers: list[Subscriber] = []
    for entry in subscribers_raw.get("subscribers", []):
        channels = [
            ChannelConfig(type=c["type"], options={k: v for k, v in c.items() if k != "type"})
            for c in entry.get("channels", [])
        ]
        f = entry.get("filters", {}) or {}
        filters = SubscriberFilters(
            regions=list(f.get("regions", [])),
            retailers=list(f.get("retailers", [])),
            kinds=list(f.get("kinds", [])),
            keywords=list(f.get("keywords", [])),
            min_confidence=float(f.get("min_confidence", 0.0)),
            max_per_day=int(f.get("max_per_day", 20)),
            quiet_hours=f.get("quiet_hours"),
        )
        raw_reminders = entry.get("reminders_minutes") or []
        try:
            reminders_minutes = [int(x) for x in raw_reminders if int(x) > 0]
        except (TypeError, ValueError):
            log.warning(
                "subscriber %r has malformed reminders_minutes=%r; ignoring",
                entry.get("id"), raw_reminders,
            )
            reminders_minutes = []

        subscribers.append(
            Subscriber(
                id=entry["id"],
                enabled=bool(entry.get("enabled", True)),
                channels=channels,
                filters=filters,
                reminders_minutes=reminders_minutes,
            )
        )

    return AppConfig(
        sources=sources,
        subscribers=subscribers,
        state_path=state_path,
        timezone=timezone_name,
    )
