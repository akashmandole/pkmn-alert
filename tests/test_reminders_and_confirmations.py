"""End-to-end tests for the reminder + confirmation-labeling features.

Uses the RecordingNotifier from test_dispatch.py to verify what actually
gets delivered (and re-delivered) as the pipeline exercises reminders
and cross-source confirmation upgrades.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pkmn_alert import state as statemod
from pkmn_alert.config import ChannelConfig, Subscriber, SubscriberFilters
from pkmn_alert.dispatch import (
    CONFIRMATION_LOOKBACK,
    dispatch,
    process_due_reminders,
)
from pkmn_alert.event import DropEvent
from pkmn_alert.notifiers.base import Notifier
from pkmn_alert.state import State


class RecordingNotifier(Notifier):
    """Records every send. Instances are collected on the class so tests
    can inspect them without wiring subscriber → instance lookup."""

    all_instances: list["RecordingNotifier"] = []

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.label = f"rec[{options.get('name', '?')}]"
        self.sent: list[tuple[str, str]] = []
        self.__class__.all_instances.append(self)

    def send(self, event: DropEvent, subscriber_id: str) -> bool:
        self.sent.append((subscriber_id, event.title))
        return True


@pytest.fixture(autouse=True)
def _reset_recorder():
    RecordingNotifier.all_instances = []
    yield
    RecordingNotifier.all_instances = []


@pytest.fixture
def route_recording(monkeypatch):
    """Route the 'recording' channel type through RecordingNotifier."""
    import pkmn_alert.notifiers as pkg

    original = pkg.build

    def build(cfg):
        if cfg.type == "recording":
            return RecordingNotifier(cfg.options)
        return original(cfg)

    monkeypatch.setattr("pkmn_alert.dispatch.build_notifier", build)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


T0 = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)


def _event(**over):
    base = dict(
        source="reddit",
        title="Pokemon Center Prismatic ETB restock live",
        url="https://example.com/x",
        detected_at=T0,
        kind="restock",
        region="US",
        retailer="pokemoncenter",
    )
    base.update(over)
    return DropEvent(**base)


def _sub(
    sub_id: str,
    *,
    reminders_minutes: list[int] | None = None,
    filters: SubscriberFilters | None = None,
    channel_name: str = "n1",
) -> Subscriber:
    return Subscriber(
        id=sub_id,
        enabled=True,
        channels=[ChannelConfig(type="recording", options={"name": channel_name})],
        filters=filters or SubscriberFilters(),
        reminders_minutes=list(reminders_minutes or []),
    )


# ---------------------------------------------------------------------------
# Confidence labeling
# ---------------------------------------------------------------------------


class TestConfidenceLabels:
    def test_reddit_alone_is_MED(self, route_recording):
        state = State()
        subs = [_sub("me")]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        rec = RecordingNotifier.all_instances[0]
        assert rec.sent == [("me", "[MED] Pokemon Center Prismatic ETB restock live")]

    def test_reddit_with_queueit_in_same_batch_is_HIGH(self, route_recording):
        state = State()
        subs = [_sub("me")]

        queueit_event = _event(source="queueit", kind="queue", title="pokemoncenter queue is live")
        reddit_event = _event(source="reddit", kind="queue", title="Queue is LIVE on Pokemon Center")

        # Same batch: queueit gets absorbed first as confirmation,
        # then reddit dispatch reads confirmation → HIGH.
        result = dispatch(
            [queueit_event, reddit_event], subs,
            now=T0, tz_name="UTC", state=state,
        )

        assert result.confirmations_recorded == 1
        rec = RecordingNotifier.all_instances[0]
        assert len(rec.sent) == 1, "queueit event must NOT dispatch on its own"
        assert rec.sent[0] == ("me", "[HIGH] Queue is LIVE on Pokemon Center")

    def test_reddit_after_recent_confirmation_is_HIGH(self, route_recording):
        state = State()
        # An earlier tick recorded confirmation; now reddit fires alone.
        state.record_confirmation("pokemoncenter", "queue", T0 - timedelta(minutes=10))

        subs = [_sub("me")]
        dispatch(
            [_event(kind="queue", title="Queue live on Pokemon Center")],
            subs, now=T0, tz_name="UTC", state=state,
        )
        assert RecordingNotifier.all_instances[0].sent == [
            ("me", "[HIGH] Queue live on Pokemon Center")
        ]

    def test_stale_confirmation_does_not_upgrade(self, route_recording):
        state = State()
        # Confirmation is older than CONFIRMATION_LOOKBACK.
        state.record_confirmation(
            "pokemoncenter", "queue",
            T0 - CONFIRMATION_LOOKBACK - timedelta(minutes=1),
        )

        subs = [_sub("me")]
        dispatch(
            [_event(kind="queue", title="Queue live on Pokemon Center")],
            subs, now=T0, tz_name="UTC", state=state,
        )
        # Old confirmation shouldn't count → MED.
        assert RecordingNotifier.all_instances[0].sent == [
            ("me", "[MED] Queue live on Pokemon Center")
        ]

    def test_queueit_alone_dispatches_nothing(self, route_recording):
        state = State()
        subs = [_sub("me")]

        result = dispatch(
            [_event(source="queueit", kind="queue", title="pokemoncenter queue live")],
            subs, now=T0, tz_name="UTC", state=state,
        )

        assert result.delivered == 0
        assert result.confirmations_recorded == 1
        assert RecordingNotifier.all_instances == [], (
            "queueit alone should not even instantiate a notifier for delivery"
        )


# ---------------------------------------------------------------------------
# Reminder queueing + firing
# ---------------------------------------------------------------------------


class TestReminders:
    def test_dispatch_queues_reminders(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]

        result = dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.reminders_queued == 1
        assert len(state.pending_reminders) == 1

        r = state.pending_reminders[0]
        assert r["subscriber_id"] == "me"
        assert r["channel"]["type"] == "recording"
        expected_due = (T0 + timedelta(minutes=15)).isoformat()
        assert r["due_iso"] == expected_due
        assert r["attempt"] == 1
        assert r["event"]["title"] == "Pokemon Center Prismatic ETB restock live"

    def test_process_reminders_before_due_fires_nothing(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        # 10 min later — reminder isn't due for another 5 min.
        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=10), tz_name="UTC",
        )
        assert fired == 0
        assert len(state.pending_reminders) == 1

        # First-instance notifier should only have the original push.
        rec = RecordingNotifier.all_instances[0]
        assert len(rec.sent) == 1

    def test_process_reminders_at_due_fires_reminder(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        # 15 min later exactly — reminder is due.
        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=15), tz_name="UTC",
        )
        assert fired == 1
        assert state.pending_reminders == []

        # The reminder rebuilds the notifier from the snapshot, which
        # creates a NEW RecordingNotifier instance. So we assert on the
        # second instance's sent list.
        assert len(RecordingNotifier.all_instances) == 2
        original_notifier, reminder_notifier = RecordingNotifier.all_instances
        assert original_notifier.sent == [
            ("me", "[MED] Pokemon Center Prismatic ETB restock live")
        ]
        assert reminder_notifier.sent == [
            ("me", "[MED][REMIND #1] Pokemon Center Prismatic ETB restock live")
        ]

    def test_reminder_relabels_to_HIGH_after_later_confirmation(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]

        # Tick T: Reddit posts a drop, but queueit hasn't seen it yet -> MED.
        dispatch([_event(kind="queue")], subs, now=T0, tz_name="UTC", state=state)
        assert RecordingNotifier.all_instances[0].sent == [
            ("me", "[MED] Pokemon Center Prismatic ETB restock live")
        ]

        # 5 minutes later, queueit sees the queue → confirmation recorded.
        state.record_confirmation("pokemoncenter", "queue", T0 + timedelta(minutes=5))

        # 15 min after original → reminder fires, and at fire time it
        # re-evaluates the label. The confirmation is now recent (10 min
        # old) so the reminder goes out as HIGH.
        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=15), tz_name="UTC",
        )
        assert fired == 1
        reminder_notifier = RecordingNotifier.all_instances[1]
        assert reminder_notifier.sent == [
            ("me", "[HIGH][REMIND #1] Pokemon Center Prismatic ETB restock live")
        ]

    def test_reminder_skipped_if_much_too_late(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        # 45 min late — REMINDER_MAX_LATENESS is 15 min. Skip.
        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=15) + timedelta(minutes=45),
            tz_name="UTC",
        )
        assert fired == 0
        assert state.pending_reminders == [], "stale reminders should be dropped"

    def test_reminder_skipped_if_subscriber_removed(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        # Subscriber list has changed — "me" no longer exists.
        fired = process_due_reminders(
            state, subscribers=[], now=T0 + timedelta(minutes=15), tz_name="UTC",
        )
        assert fired == 0
        assert state.pending_reminders == []

    def test_reminder_dry_run_does_not_call_send(self, route_recording):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)

        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=15), tz_name="UTC", dry_run=True,
        )
        assert fired == 1
        # dry_run: a fresh notifier IS built (to verify it's still
        # configured — a real deploy would want to catch e.g. a rotated
        # ntfy topic before pretending to fire), but its send() must not
        # be called. Verify by checking the second instance's sent list
        # is empty while the first still holds only the original push.
        assert len(RecordingNotifier.all_instances) == 2
        original_notifier, reminder_notifier = RecordingNotifier.all_instances
        assert original_notifier.sent == [
            ("me", "[MED] Pokemon Center Prismatic ETB restock live")
        ]
        assert reminder_notifier.sent == [], "dry_run must not actually send"


# ---------------------------------------------------------------------------
# State serialization
# ---------------------------------------------------------------------------


class TestStateRoundTrip:
    def test_pending_reminders_survive_save_load(self, route_recording, tmp_path):
        state = State()
        subs = [_sub("me", reminders_minutes=[15])]
        dispatch([_event()], subs, now=T0, tz_name="UTC", state=state)
        assert len(state.pending_reminders) == 1

        path = tmp_path / "state.json"
        statemod.save(path, state)

        # Round-trip through disk.
        loaded = statemod.load(path)
        assert len(loaded.pending_reminders) == 1
        # Structure preserved.
        r = loaded.pending_reminders[0]
        assert r["subscriber_id"] == "me"
        assert r["channel"]["type"] == "recording"
        assert r["event"]["retailer"] == "pokemoncenter"

        # And firing from the loaded state works exactly like the original.
        fired = process_due_reminders(
            loaded, subs, now=T0 + timedelta(minutes=15), tz_name="UTC",
        )
        assert fired == 1

    def test_confirmations_survive_save_load(self, tmp_path):
        state = State()
        state.record_confirmation("pokemoncenter", "queue", T0)
        assert state.was_recently_confirmed(
            "pokemoncenter", "queue", CONFIRMATION_LOOKBACK, T0 + timedelta(minutes=5)
        )

        path = tmp_path / "state.json"
        statemod.save(path, state)
        loaded = statemod.load(path)

        assert loaded.was_recently_confirmed(
            "pokemoncenter", "queue", CONFIRMATION_LOOKBACK, T0 + timedelta(minutes=5)
        )

    def test_prune_drops_expired_confirmations(self):
        state = State()
        state.record_confirmation("old_retailer", "queue", T0 - timedelta(hours=25))
        state.record_confirmation("pokemoncenter", "queue", T0 - timedelta(hours=1))
        state.prune(T0)

        assert "old_retailer_queue" not in state.recent_confirmations
        assert "pokemoncenter_queue" in state.recent_confirmations
