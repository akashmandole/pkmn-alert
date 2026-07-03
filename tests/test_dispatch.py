"""End-to-end dispatch test using an in-memory notifier."""

from datetime import datetime, timezone
from typing import Any

from pkmn_alert.config import ChannelConfig, Subscriber, SubscriberFilters
from pkmn_alert.dispatch import dispatch
from pkmn_alert.event import DropEvent
from pkmn_alert.notifiers.base import Notifier


class RecordingNotifier(Notifier):
    """Test double that records every send. Injected via monkey-patching."""

    all_instances: list["RecordingNotifier"] = []

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.label = f"recording[{options.get('name', '?')}]"
        self.sent: list[tuple[str, str]] = []  # (subscriber_id, event_title)
        self.__class__.all_instances.append(self)

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        self.sent.append((subscriber_id, event.title))
        return True


def _event(**over):
    base = dict(
        source="reddit",
        title="Pokemon Center Prismatic ETB restock live",
        url="",
        detected_at=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
        kind="restock",
        region="US",
        retailer="pokemoncenter",
    )
    base.update(over)
    return DropEvent(**base)


def _sub(sub_id: str, filters: SubscriberFilters, name: str = "n1") -> Subscriber:
    return Subscriber(
        id=sub_id,
        enabled=True,
        channels=[ChannelConfig(type="recording", options={"name": name})],
        filters=filters,
    )


def _install_recording_notifier(monkeypatch):
    # Route the "recording" channel type through our test class.
    import pkmn_alert.notifiers as pkg
    original = pkg.build
    RecordingNotifier.all_instances = []

    def build(cfg):
        if cfg.type == "recording":
            return RecordingNotifier(cfg.options)
        return original(cfg)

    monkeypatch.setattr("pkmn_alert.dispatch.build_notifier", build)


class TestDispatch:
    def test_delivers_to_matching_subscriber(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        events = [_event()]

        result = dispatch(events, subs, now=datetime(2026, 7, 1, 20, tzinfo=timezone.utc), tz_name="UTC")

        assert result.delivered == 1
        assert result.failed == 0
        # No state passed => dispatcher falls back to MED (backwards-compat mode).
        assert RecordingNotifier.all_instances[0].sent == [("me", "[MED] Pokemon Center Prismatic ETB restock live")]

    def test_filters_prevent_delivery(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        subs = [_sub("me", SubscriberFilters(retailers=["target"]))]
        events = [_event(retailer="pokemoncenter")]

        result = dispatch(events, subs, now=datetime(2026, 7, 1, 20, tzinfo=timezone.utc), tz_name="UTC")

        assert result.delivered == 0
        assert result.skipped_filtered == 1

    def test_dry_run_does_not_send(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        subs = [_sub("me", SubscriberFilters())]

        result = dispatch(
            [_event()],
            subs,
            now=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
            tz_name="UTC",
            dry_run=True,
        )

        assert result.delivered == 1
        assert RecordingNotifier.all_instances[0].sent == [], "dry_run must not actually call send()"

    def test_multiple_subscribers_get_the_same_event(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        subs = [
            _sub("me", SubscriberFilters(), name="me-n"),
            _sub("friend", SubscriberFilters(), name="friend-n"),
        ]

        result = dispatch(
            [_event()],
            subs,
            now=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
            tz_name="UTC",
        )

        assert result.delivered == 2
        # Order of instances: subscriber "me" then "friend"
        assert RecordingNotifier.all_instances[0].sent == [("me", "[MED] Pokemon Center Prismatic ETB restock live")]
        assert RecordingNotifier.all_instances[1].sent == [("friend", "[MED] Pokemon Center Prismatic ETB restock live")]

    def test_disabled_subscriber_gets_nothing(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        s = _sub("me", SubscriberFilters())
        s.enabled = False

        result = dispatch(
            [_event()],
            [s],
            now=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
            tz_name="UTC",
        )
        assert result.delivered == 0
