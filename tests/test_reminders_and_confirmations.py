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
    require_confirmation: bool = False,
) -> Subscriber:
    return Subscriber(
        id=sub_id,
        enabled=True,
        channels=[ChannelConfig(type="recording", options={"name": channel_name})],
        filters=filters or SubscriberFilters(),
        reminders_minutes=list(reminders_minutes or []),
        require_confirmation=require_confirmation,
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


# ---------------------------------------------------------------------------
# require_confirmation gate (Option B — false-positive lockdown)
# ---------------------------------------------------------------------------


class TestRequireConfirmationGate:
    """When a subscriber sets ``require_confirmation=True`` they only want
    events physically verified on pokemoncenter.com. MED events (Reddit-only)
    must be dropped silently and MUST NOT mark the drop as suppressed —
    otherwise a subsequent tick that DOES land a queueit confirmation
    would be swallowed by cross-tick dedupe."""

    # ---- initial dispatch ----

    def test_med_event_skipped_for_require_confirmation_subscriber(self, route_recording):
        state = State()
        subs = [_sub("me", require_confirmation=True)]

        result = dispatch([_event(kind="queue")], subs, now=T0, tz_name="UTC", state=state)

        assert result.delivered == 0
        assert result.skipped_awaiting_confirmation == 1
        assert result.skipped_filtered == 0, (
            "the gate runs BEFORE filters, so filter counter must stay clean"
        )
        assert RecordingNotifier.all_instances[0].sent == []

    def test_high_event_delivered_for_require_confirmation_subscriber(self, route_recording):
        state = State()
        # Prior queueit confirmation on file → Reddit event this tick is HIGH.
        state.record_confirmation("pokemoncenter", "queue", T0 - timedelta(minutes=10))
        subs = [_sub("me", require_confirmation=True)]

        result = dispatch(
            [_event(kind="queue", title="Queue is LIVE on Pokemon Center")],
            subs, now=T0, tz_name="UTC", state=state,
        )

        assert result.delivered == 1
        assert result.skipped_awaiting_confirmation == 0
        assert RecordingNotifier.all_instances[0].sent == [
            ("me", "[HIGH] Queue is LIVE on Pokemon Center")
        ]

    def test_same_batch_queueit_upgrades_reddit_for_require_confirmation(self, route_recording):
        """The intended happy path: queueit and Reddit fire in the same
        tick, queueit is absorbed as confirmation first, then Reddit
        dispatches as HIGH and passes the gate."""
        state = State()
        subs = [_sub("me", require_confirmation=True)]

        events = [
            _event(source="queueit", kind="queue", title="pokemoncenter queue live"),
            _event(source="reddit", kind="queue", title="Queue is LIVE on Pokemon Center"),
        ]

        result = dispatch(events, subs, now=T0, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.confirmations_recorded == 1
        assert result.skipped_awaiting_confirmation == 0

    def test_med_skip_does_not_mark_drop_alerted(self, route_recording):
        """Critical invariant: if we drop a MED event because the sub is
        awaiting confirmation, we MUST NOT record it in ``alerted_drops``.
        Otherwise the next tick's HIGH event (queueit finally saw it) would
        be cross-tick-suppressed and the user would still hear nothing."""
        state = State()
        subs = [_sub("me", require_confirmation=True)]

        # Tick 1: Reddit MED — silently dropped.
        dispatch(
            [_event(kind="queue", title="PC Queue is up")],
            subs, now=T0, tz_name="UTC", state=state,
        )
        assert state.alerted_drops == {}, "skipped-awaiting-confirmation must not suppress"

        # Tick 2 (2 min later): queueit records confirmation, then a
        # fresh Reddit post about the same drop dispatches as HIGH.
        t2 = T0 + timedelta(minutes=2)
        result = dispatch(
            [
                _event(source="queueit", kind="queue", title="pokemoncenter queue live", detected_at=t2),
                _event(kind="queue", title="PC Queue confirmed live", detected_at=t2),
            ],
            subs, now=t2, tz_name="UTC", state=state,
        )
        assert result.delivered == 1
        assert "pokemoncenter:queue" in state.alerted_drops

    def test_mixed_subscribers_only_lock_down_the_flagged_one(self, route_recording):
        """A `debug-stdout` style subscriber without the flag still sees
        MED events; the flagged subscriber does not."""
        state = State()
        subs = [
            _sub("me",    require_confirmation=True,  channel_name="me-n"),
            _sub("debug", require_confirmation=False, channel_name="debug-n"),
        ]

        result = dispatch([_event(kind="queue", title="PC Queue is up")], subs, now=T0, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.skipped_awaiting_confirmation == 1
        # Instance ordering follows subscriber ordering.
        me_notifier, debug_notifier = RecordingNotifier.all_instances
        assert me_notifier.sent == []
        assert debug_notifier.sent == [("debug", "[MED] PC Queue is up")]

    # ---- reminders ----

    def test_reminder_dropped_if_still_med_at_fire_time(self, route_recording):
        """A reminder queued when there WAS a confirmation must be dropped
        (not fired MED) if the confirmation has since aged out and the
        subscriber requires HIGH-only."""
        state = State()
        # Confirmation exists → the primary send goes through as HIGH
        # and schedules a reminder.
        state.record_confirmation("pokemoncenter", "queue", T0)
        subs = [_sub("me", require_confirmation=True, reminders_minutes=[15])]

        dispatch(
            [_event(kind="queue", title="Queue live on PC")],
            subs, now=T0, tz_name="UTC", state=state,
        )
        assert len(state.pending_reminders) == 1

        # Fast-forward past CONFIRMATION_LOOKBACK so the reminder
        # re-evaluates as MED. With require_confirmation=True it must
        # be dropped, not fired.
        fire_at = T0 + CONFIRMATION_LOOKBACK + timedelta(minutes=1)
        fired = process_due_reminders(state, subs, now=fire_at, tz_name="UTC")

        assert fired == 0
        assert state.pending_reminders == []
        # Only the original send exists on the wire.
        assert RecordingNotifier.all_instances[0].sent == [
            ("me", "[HIGH] Queue live on PC")
        ]

    def test_reminder_fires_if_still_high_at_fire_time(self, route_recording):
        """Reminder should still fire if the confirmation is still fresh."""
        state = State()
        state.record_confirmation("pokemoncenter", "queue", T0)
        subs = [_sub("me", require_confirmation=True, reminders_minutes=[15])]

        dispatch(
            [_event(kind="queue", title="Queue live on PC")],
            subs, now=T0, tz_name="UTC", state=state,
        )

        # Fire the reminder 15 min later. Confirmation is 15 min old,
        # which is inside CONFIRMATION_LOOKBACK → still HIGH.
        fired = process_due_reminders(
            state, subs, now=T0 + timedelta(minutes=15), tz_name="UTC",
        )
        assert fired == 1
        _, reminder_notifier = RecordingNotifier.all_instances
        assert reminder_notifier.sent == [
            ("me", "[HIGH][REMIND #1] Queue live on PC")
        ]


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestRequireConfirmationConfig:
    """The YAML field must round-trip into the Subscriber dataclass and
    default False when omitted (backward compat for existing files)."""

    def test_default_is_false_when_field_omitted(self, tmp_path):
        from pkmn_alert import config as cfgmod

        subs_yaml = tmp_path / "subs.yaml"
        subs_yaml.write_text(
            "subscribers:\n"
            "  - id: legacy\n"
            "    enabled: true\n"
            "    channels: []\n"
            "    filters: {}\n",
        )
        srcs_yaml = tmp_path / "srcs.yaml"
        srcs_yaml.write_text("sources: []\n")

        app = cfgmod.load(
            sources_path=srcs_yaml,
            subscribers_path=subs_yaml,
            state_path=tmp_path / "state.json",
        )
        assert app.subscribers[0].require_confirmation is False

    def test_true_is_parsed_from_yaml(self, tmp_path):
        from pkmn_alert import config as cfgmod

        subs_yaml = tmp_path / "subs.yaml"
        subs_yaml.write_text(
            "subscribers:\n"
            "  - id: strict\n"
            "    enabled: true\n"
            "    channels: []\n"
            "    filters: {}\n"
            "    require_confirmation: true\n",
        )
        srcs_yaml = tmp_path / "srcs.yaml"
        srcs_yaml.write_text("sources: []\n")

        app = cfgmod.load(
            sources_path=srcs_yaml,
            subscribers_path=subs_yaml,
            state_path=tmp_path / "state.json",
        )
        assert app.subscribers[0].require_confirmation is True
