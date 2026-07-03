from datetime import datetime, timezone

from pkmn_alert.config import SubscriberFilters
from pkmn_alert.event import DropEvent
from pkmn_alert.filters import matches


def _event(**over):
    base = dict(
        source="reddit",
        title="Pokemon Center Prismatic ETB restock live",
        url="",
        detected_at=datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc),
        kind="restock",
        region="US",
        retailer="pokemoncenter",
        confidence=1.0,
    )
    base.update(over)
    return DropEvent(**base)


def _at(hh: int, mm: int = 0) -> datetime:
    # A moment in America/Los_Angeles converted to UTC. July 1 2026 is PDT
    # (UTC-7), so 22:00 local = 05:00 UTC next day.
    from zoneinfo import ZoneInfo
    return datetime(2026, 7, 1, hh, mm, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)


NOW = _at(12)
TZ = "America/Los_Angeles"


class TestBasicFilters:
    def test_no_filters_allows_everything(self):
        ok, reason = matches(_event(), SubscriberFilters(), NOW, TZ)
        assert ok is True and reason == ""

    def test_region_allowlist(self):
        f = SubscriberFilters(regions=["US"])
        assert matches(_event(region="US"), f, NOW, TZ)[0]
        assert not matches(_event(region="UK"), f, NOW, TZ)[0]

    def test_retailer_allowlist(self):
        f = SubscriberFilters(retailers=["pokemoncenter"])
        assert matches(_event(), f, NOW, TZ)[0]
        assert not matches(_event(retailer="target"), f, NOW, TZ)[0]

    def test_keyword_any_of(self):
        f = SubscriberFilters(keywords=["Prismatic", "surging"])
        assert matches(_event(title="Prismatic Evolutions restock"), f, NOW, TZ)[0]
        assert matches(_event(title="Surging Sparks preorder"), f, NOW, TZ)[0]
        assert not matches(_event(title="151 booster bundle"), f, NOW, TZ)[0]

    def test_min_confidence(self):
        f = SubscriberFilters(min_confidence=0.8)
        assert matches(_event(confidence=1.0), f, NOW, TZ)[0]
        assert not matches(_event(confidence=0.5), f, NOW, TZ)[0]


class TestQuietHours:
    def test_daytime_not_muted(self):
        f = SubscriberFilters(quiet_hours="22:00-06:00")
        assert matches(_event(kind="restock"), f, _at(14), TZ)[0]

    def test_overnight_range_mutes_restock(self):
        f = SubscriberFilters(quiet_hours="22:00-06:00")
        ok, reason = matches(_event(kind="restock"), f, _at(2), TZ)
        assert not ok and "quiet" in reason

    def test_queue_events_bypass_quiet_hours(self):
        f = SubscriberFilters(quiet_hours="22:00-06:00")
        # Even at 2am, a QUEUE alert fires — that's the whole point of the tool.
        ok, _ = matches(_event(kind="queue"), f, _at(2), TZ)
        assert ok

    def test_unparseable_quiet_hours_fails_open(self):
        f = SubscriberFilters(quiet_hours="not-a-time")
        ok, _ = matches(_event(), f, NOW, TZ)
        assert ok, "malformed quiet_hours must not silently swallow all alerts"
