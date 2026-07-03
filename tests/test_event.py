from datetime import datetime, timezone

from pkmn_alert.event import DropEvent


def _event(**over):
    base = dict(
        source="reddit",
        title="Pokemon Center Prismatic ETB restock live!",
        url="https://example.com/x",
        detected_at=datetime(2026, 7, 1, 20, 14, 12, tzinfo=timezone.utc),
    )
    base.update(over)
    return DropEvent(**base)


class TestDedupeKey:
    def test_identical_titles_collapse(self):
        a = _event()
        b = _event(url="https://different.com")
        assert a.dedupe_key() == b.dedupe_key(), (
            "url should not affect dedupe (same drop reported by different sources)"
        )

    def test_whitespace_and_case_normalized(self):
        a = _event(title="POKEMON CENTER   Prismatic ETB  RESTOCK LIVE!")
        b = _event(title="pokemon center prismatic etb restock live!")
        assert a.dedupe_key() == b.dedupe_key()

    def test_different_kinds_do_not_collapse(self):
        a = _event(kind="queue")
        b = _event(kind="restock")
        assert a.dedupe_key() != b.dedupe_key(), (
            "kind must be part of the key: a queue event and a restock event "
            "with identical titles are genuinely different signals"
        )

    def test_detected_at_does_not_affect_key(self):
        a = _event()
        b = _event(detected_at=datetime(2001, 1, 1, tzinfo=timezone.utc))
        assert a.dedupe_key() == b.dedupe_key()

    def test_key_is_stable_length(self):
        assert len(_event().dedupe_key()) == 16


class TestSerialization:
    def test_to_dict_round_trip_shape(self):
        d = _event().to_dict()
        assert d["source"] == "reddit"
        assert d["kind"] == "restock"
        assert d["detected_at"].startswith("2026-07-01T20:14:12")
        assert "raw" not in d, "raw is intentionally omitted from wire format"
