from datetime import datetime, timedelta, timezone

from pkmn_alert import state as statemod


def test_load_missing_file_returns_empty(tmp_path):
    s = statemod.load(tmp_path / "does-not-exist.json")
    assert s.seen == {} and s.cooldowns == {} and s.source_meta == {}


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    s = statemod.State()
    now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
    s.mark_seen("abc123", now)
    s.set_cooldown("queueit", now + timedelta(minutes=30))
    s.source_meta.setdefault("reddit", {})["last_id_PokemonRestocks"] = "t3_abc001"
    statemod.save(path, s)

    loaded = statemod.load(path)
    assert loaded.has_seen("abc123")
    assert loaded.is_in_cooldown("queueit", now + timedelta(minutes=10))
    assert not loaded.is_in_cooldown("queueit", now + timedelta(minutes=45))
    assert loaded.source_meta["reddit"]["last_id_PokemonRestocks"] == "t3_abc001"


def test_prune_drops_entries_older_than_ttl():
    s = statemod.State()
    now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
    # 25 hours old -> expired
    s.seen["expired"] = (now - timedelta(hours=25)).isoformat()
    # 1 hour old -> kept
    s.seen["fresh"] = (now - timedelta(hours=1)).isoformat()
    s.prune(now)
    assert "expired" not in s.seen
    assert "fresh" in s.seen


def test_prune_caps_size():
    s = statemod.State()
    now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(statemod.MAX_ENTRIES + 100):
        # Older entries have smaller offsets, so sorting keeps the newest.
        s.seen[f"key-{i:04d}"] = (now - timedelta(seconds=(600 - i))).isoformat()
    s.prune(now)
    assert len(s.seen) == statemod.MAX_ENTRIES


def test_expired_cooldowns_dropped_on_prune():
    s = statemod.State()
    now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
    s.set_cooldown("queueit", now - timedelta(minutes=5))  # already expired
    s.set_cooldown("reddit", now + timedelta(minutes=10))  # still active
    s.prune(now)
    assert "queueit" not in s.cooldowns
    assert "reddit" in s.cooldowns


class TestDropSuppression:
    """State-level tests for the ``alerted_drops`` field that backs
    the dispatcher's per-drop suppression window."""

    def test_drop_key_normalizes_case(self):
        # Two events describing the same drop should produce the same
        # key regardless of upstream casing.
        assert statemod.State.drop_key("PokemonCenter", "Queue") == "pokemoncenter:queue"
        assert statemod.State.drop_key("pokemoncenter", "queue") == "pokemoncenter:queue"

    def test_drop_key_handles_missing_fields(self):
        # Retailer/kind occasionally come through empty; we want a
        # stable placeholder rather than a KeyError or ``":"``.
        assert statemod.State.drop_key("", "") == "unknown:unknown"
        assert statemod.State.drop_key("pokemoncenter", "") == "pokemoncenter:unknown"

    def test_was_recently_alerted_false_when_never_marked(self):
        s = statemod.State()
        now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        assert not s.was_drop_recently_alerted(
            "pokemoncenter:queue", timedelta(minutes=60), now,
        )

    def test_was_recently_alerted_true_inside_window(self):
        s = statemod.State()
        now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        s.mark_drop_alerted("pokemoncenter:queue", now - timedelta(minutes=10))
        assert s.was_drop_recently_alerted(
            "pokemoncenter:queue", timedelta(minutes=60), now,
        )

    def test_was_recently_alerted_false_after_window(self):
        s = statemod.State()
        now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        s.mark_drop_alerted("pokemoncenter:queue", now - timedelta(minutes=70))
        assert not s.was_drop_recently_alerted(
            "pokemoncenter:queue", timedelta(minutes=60), now,
        )

    def test_mark_alerted_overwrites_earlier_timestamp(self):
        """Second alert extends suppression from the LATER time — otherwise
        a slow trickle of posts would keep re-alerting every hour on the dot."""
        s = statemod.State()
        base = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        s.mark_drop_alerted("pokemoncenter:queue", base)
        s.mark_drop_alerted("pokemoncenter:queue", base + timedelta(minutes=10))

        # 65 min after the FIRST mark but only 55 min after the LAST:
        # should still be suppressed.
        assert s.was_drop_recently_alerted(
            "pokemoncenter:queue",
            timedelta(minutes=60),
            base + timedelta(minutes=65),
        )

    def test_expired_alerted_drops_pruned(self):
        s = statemod.State()
        now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        s.mark_drop_alerted("stale:queue", now - timedelta(hours=3))
        s.mark_drop_alerted("fresh:queue", now - timedelta(minutes=30))
        s.prune(now)
        assert "stale:queue" not in s.alerted_drops
        assert "fresh:queue" in s.alerted_drops

    def test_save_load_round_trips_alerted_drops(self, tmp_path):
        path = tmp_path / "state.json"
        s = statemod.State()
        now = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)
        s.mark_drop_alerted("pokemoncenter:queue", now)
        statemod.save(path, s)

        loaded = statemod.load(path)
        assert loaded.was_drop_recently_alerted(
            "pokemoncenter:queue", timedelta(minutes=60), now + timedelta(minutes=5),
        )
