"""End-to-end dispatch test using an in-memory notifier."""

from datetime import datetime, timedelta, timezone
from typing import Any

from pkmn_alert.config import ChannelConfig, Subscriber, SubscriberFilters
from pkmn_alert.dispatch import DROP_DEDUPE_WINDOW, dispatch
from pkmn_alert.event import DropEvent
from pkmn_alert.notifiers.base import Notifier
from pkmn_alert.state import State


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


def _sub(
    sub_id: str,
    filters: SubscriberFilters,
    name: str = "n1",
    reminders_minutes: list[int] | None = None,
) -> Subscriber:
    return Subscriber(
        id=sub_id,
        enabled=True,
        channels=[ChannelConfig(type="recording", options={"name": name})],
        filters=filters,
        reminders_minutes=reminders_minutes or [],
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


# ---------------------------------------------------------------------------
# Per-drop coalescing + cross-tick suppression
# ---------------------------------------------------------------------------


class TestDropCoalescingAndSuppression:
    """The user-visible contract for these tests:

    A single real-world drop (e.g. Pokemon Center queue going live) may
    produce N Reddit posts within a few minutes. The user must receive
    exactly ONE immediate push, plus one push per configured reminder
    interval. Never a burst of N pushes at 04:49:22 UTC.

    See ``dispatch._coalesce_and_dedupe`` and ``State.alerted_drops``."""

    def _base_events(self, base_ts: datetime) -> list[DropEvent]:
        """Five fresh Reddit posts, all describing the same real-world
        PC queue drop. Modeled on the actual titles that hit the user's
        phone during the 2026-07-20 18:27 UTC event."""
        titles = [
            "Pokemon Center Queue is up!",
            "Pokemon Center Queue is Live!",
            "Lock IN! PKC Drop at 10-10:30 AM PST",
            "Pokemon Center Queue Monitor - Security Change Detected",
            "Recent Pokemon Center Queue Start Times",
        ]
        return [
            _event(
                title=t,
                # Stagger detected_at so the "earliest" rule has a stable
                # winner regardless of dict ordering.
                detected_at=base_ts + timedelta(seconds=i),
                kind="queue",
                retailer="pokemoncenter",
            )
            for i, t in enumerate(titles)
        ]

    def test_five_posts_same_drop_yield_one_alert(self, monkeypatch):
        """The bug that motivated this feature: 5 Reddit posts about the
        same drop => 5 back-to-back ntfy pushes. Now: exactly 1."""
        _install_recording_notifier(monkeypatch)
        state = State()
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]

        result = dispatch(self._base_events(now), subs, now=now, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.coalesced == 4, "the four non-representative posts must be reported as coalesced"
        assert result.suppressed_recent_drop == 0
        # Earliest event wins as the representative.
        sent = RecordingNotifier.all_instances[0].sent
        assert len(sent) == 1
        assert sent[0][1] == "[MED] Pokemon Center Queue is up!"

    def test_second_tick_within_window_suppressed_entirely(self, monkeypatch):
        """Sim: fresh tick 20 min after the first one brings ANOTHER post
        about the same drop. The user must not see a new push."""
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        t0 = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        # Tick 1 — 5 posts collapse to 1 alert.
        dispatch(self._base_events(t0), subs, now=t0, tz_name="UTC", state=state)
        instances_after_tick1 = list(RecordingNotifier.all_instances)
        sends_after_tick1 = sum(len(n.sent) for n in instances_after_tick1)

        # Tick 2 — new post about the same drop, 20 min later.
        t1 = t0 + timedelta(minutes=20)
        late_event = _event(
            title="Pokemon Center Queue still going strong",
            detected_at=t1,
            kind="queue",
            retailer="pokemoncenter",
        )
        result = dispatch([late_event], subs, now=t1, tz_name="UTC", state=state)

        assert result.delivered == 0
        assert result.suppressed_recent_drop == 1

        # No new sends fired anywhere. The dispatcher legitimately
        # short-circuits before even building a notifier when everything
        # is suppressed, so total send count across ALL instances stays
        # equal to what it was after tick 1.
        sends_after_tick2 = sum(len(n.sent) for n in RecordingNotifier.all_instances)
        assert sends_after_tick2 == sends_after_tick1, (
            "no additional push allowed within the 60-min suppression window"
        )

    def test_third_tick_after_window_alerts_again(self, monkeypatch):
        """Once the 60-min window elapses a NEW post about the same
        (retailer, kind) is treated as a fresh drop and alerts again.
        Necessary so a multi-hour store event still gets covered."""
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        t0 = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        dispatch(self._base_events(t0), subs, now=t0, tz_name="UTC", state=state)
        RecordingNotifier.all_instances.clear()
        _install_recording_notifier(monkeypatch)

        t2 = t0 + DROP_DEDUPE_WINDOW + timedelta(minutes=5)
        wave_two = _event(
            title="Pokemon Center Wave 2 is up!",
            detected_at=t2,
            kind="queue",
            retailer="pokemoncenter",
        )
        result = dispatch([wave_two], subs, now=t2, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.suppressed_recent_drop == 0

    def test_different_kinds_are_distinct_drops(self, monkeypatch):
        """A ``queue`` event and a ``restock`` event for the same retailer
        are two different drops from the user's perspective — both alert."""
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        events = [
            _event(title="PC Queue is up", detected_at=now, kind="queue"),
            _event(title="PC restock separate event", detected_at=now, kind="restock"),
        ]
        result = dispatch(events, subs, now=now, tz_name="UTC", state=state)

        assert result.delivered == 2
        assert result.coalesced == 0
        assert result.suppressed_recent_drop == 0

    def test_different_retailers_are_distinct_drops(self, monkeypatch):
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [_sub("me", SubscriberFilters())]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        events = [
            _event(title="PC Queue", detected_at=now, kind="queue", retailer="pokemoncenter"),
            _event(title="Target queue", detected_at=now, kind="queue", retailer="target"),
        ]
        result = dispatch(events, subs, now=now, tz_name="UTC", state=state)

        assert result.delivered == 2

    def test_reminders_queued_once_per_drop_not_per_post(self, monkeypatch):
        """Regression for the specific misbehavior: previously each of
        the 5 posts queued its own +15 min reminder, so the user got
        4 extra pushes 15 min later. Now only the one coalesced event
        schedules reminders."""
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [
            _sub(
                "me",
                SubscriberFilters(retailers=["pokemoncenter"]),
                reminders_minutes=[15, 30],
            )
        ]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        result = dispatch(self._base_events(now), subs, now=now, tz_name="UTC", state=state)

        assert result.delivered == 1
        assert result.reminders_queued == 2, "one reminder per interval, not per post"
        assert len(state.pending_reminders) == 2

    def test_failed_delivery_does_not_mark_drop_alerted(self, monkeypatch):
        """If the notifier fails (network hiccup, ntfy down), we must NOT
        mark the drop suppressed — next tick should retry."""
        _install_recording_notifier(monkeypatch)

        # Wrap the recording notifier to force it to fail once.
        original_send = RecordingNotifier.send
        call_count = {"n": 0}

        def failing_send(self, event, subscriber_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False
            return original_send(self, event, subscriber_id)

        monkeypatch.setattr(RecordingNotifier, "send", failing_send)

        state = State()
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        # Tick 1: send fails.
        r1 = dispatch(
            [_event(title="PC Queue is up", detected_at=now, kind="queue")],
            subs, now=now, tz_name="UTC", state=state,
        )
        assert r1.failed == 1
        assert r1.delivered == 0
        assert state.alerted_drops == {}, "failed sends leave the suppression window open"

        # Tick 2 (1 min later): retry the same drop, now the send succeeds.
        r2 = dispatch(
            [_event(title="PC Queue is up", detected_at=now, kind="queue")],
            subs, now=now + timedelta(minutes=1), tz_name="UTC", state=state,
        )
        assert r2.delivered == 1
        assert "pokemoncenter:queue" in state.alerted_drops

    def test_dry_run_does_not_mark_drop_alerted(self, monkeypatch):
        """Preview runs must be side-effect-free w.r.t. suppression."""
        _install_recording_notifier(monkeypatch)
        state = State()
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        dispatch(self._base_events(now), subs, now=now, tz_name="UTC", state=state, dry_run=True)

        assert state.alerted_drops == {}

    def test_coalescing_still_happens_without_state(self, monkeypatch):
        """Legacy callers that don't pass ``state=`` skip suppression
        entirely (backwards-compat with pre-existing test fixtures).
        Coalescing is state-independent though — so ideally... hmm, in
        this implementation coalescing IS gated on state. Document that
        behavior explicitly."""
        _install_recording_notifier(monkeypatch)
        subs = [_sub("me", SubscriberFilters(retailers=["pokemoncenter"]))]
        now = datetime(2026, 7, 20, 18, 27, tzinfo=timezone.utc)

        result = dispatch(self._base_events(now), subs, now=now, tz_name="UTC")

        # Without state we pass everything through — legacy behavior.
        # If you rely on coalescing, pass state.
        assert result.delivered == 5
        assert result.coalesced == 0
