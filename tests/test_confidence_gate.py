"""Tests for the confidence gate on the queueit source and the signal
log that powers it, plus the burst-window behavior that opens after a
queueit confirmation.

The queueit source itself needs curl_cffi installed to actually fetch;
we test the gate as pure control-flow by patching curl_cffi out."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from pkmn_alert.dispatch import PROBE_BURST_DURATION, dispatch
from pkmn_alert.event import DropEvent
from pkmn_alert.sources.base import SourceContext
from pkmn_alert.sources.queueit import (
    DEFAULT_BURST_MINUTES,
    QueueItSource,
)
from pkmn_alert.state import State

T0 = datetime(2026, 7, 1, 20, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# curl_cffi stub — the gate tests don't exercise the actual HTTP path
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_curl_cffi():
    """Insert a fake curl_cffi module so queueit.fetch() gets past its
    lazy import even in environments where the real curl_cffi isn't
    installed. Callers set .response on the returned mock to control
    what the fetch sees."""
    fake_module = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")

    class Holder:
        response = None

    def _get(url, **_kwargs):
        assert Holder.response is not None, "test forgot to set stub_curl_cffi.response"
        return Holder.response

    fake_requests.get = _get
    fake_module.requests = fake_requests

    sys.modules["curl_cffi"] = fake_module
    sys.modules["curl_cffi.requests"] = fake_requests
    yield Holder
    sys.modules.pop("curl_cffi", None)
    sys.modules.pop("curl_cffi.requests", None)


def _mk_normal_response():
    r = MagicMock()
    r.status_code = 200
    r.url = "https://www.pokemoncenter.com/"
    r.cookies = {}
    r.content = b"<html><body>Welcome to Pokemon Center</body></html>"
    r.text = r.content.decode()
    return r


def _mk_queue_response():
    r = MagicMock()
    r.status_code = 200
    r.url = "https://pokemoncenter.queue-it.net/?c=pokemoncenter"
    r.cookies = {"QueueITAccepted": "1"}
    r.content = b"<html>You're in the virtual queue. Estimated wait time 5 minutes.</html>"
    r.text = r.content.decode()
    return r


# ---------------------------------------------------------------------------
# State-level signal log
# ---------------------------------------------------------------------------


class TestSignalLog:
    def test_record_and_query_within_window(self):
        s = State()
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=5))
        s.record_signal("reddit", "restock", "pokemoncenter", T0 - timedelta(minutes=10))

        recent = s.distinct_signal_sources_since(
            since=T0 - timedelta(minutes=20), now=T0,
        )
        assert recent == {"reddit"}

    def test_signals_older_than_window_excluded(self):
        s = State()
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=30))
        recent = s.distinct_signal_sources_since(
            since=T0 - timedelta(minutes=20), now=T0,
        )
        assert recent == set()

    def test_exclude_filter(self):
        s = State()
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=5))
        s.record_signal("queueit", "queue", "pokemoncenter", T0 - timedelta(minutes=5))

        recent = s.distinct_signal_sources_since(
            since=T0 - timedelta(minutes=20), now=T0, exclude={"queueit"},
        )
        assert recent == {"reddit"}

    def test_kind_and_retailer_filters(self):
        s = State()
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=5))
        s.record_signal("reddit", "deal", "pokemoncenter", T0 - timedelta(minutes=5))
        s.record_signal("reddit", "queue", "target", T0 - timedelta(minutes=5))

        recent = s.distinct_signal_sources_since(
            since=T0 - timedelta(minutes=20), now=T0,
            kinds={"queue", "restock", "preorder"},
            retailers={"pokemoncenter", "unknown"},
        )
        assert recent == {"reddit"}

        # Change filter — target retailer now allowed, deal kind still not.
        recent = s.distinct_signal_sources_since(
            since=T0 - timedelta(minutes=20), now=T0,
            kinds={"queue"},
            retailers={"pokemoncenter", "target"},
        )
        assert recent == {"reddit"}

    def test_prune_drops_signals_older_than_60_min(self):
        s = State()
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=90))
        s.record_signal("reddit", "queue", "pokemoncenter", T0 - timedelta(minutes=5))
        s.prune(T0)
        assert len(s.signal_log) == 1

    def test_probe_burst_lifecycle(self):
        s = State()
        assert not s.is_in_probe_burst(T0)

        s.set_probe_burst(T0 + timedelta(minutes=30))
        assert s.is_in_probe_burst(T0)
        assert s.is_in_probe_burst(T0 + timedelta(minutes=15))
        assert not s.is_in_probe_burst(T0 + timedelta(minutes=45))

    def test_prune_clears_expired_burst(self):
        s = State()
        s.set_probe_burst(T0 - timedelta(minutes=1))
        s.prune(T0)
        assert s.probe_burst_until == ""


# ---------------------------------------------------------------------------
# Gate behavior in QueueItSource.fetch()
# ---------------------------------------------------------------------------


class TestGate:
    def test_gate_closed_no_signals_skips_fetch(self, stub_curl_cffi):
        state = State()
        src = QueueItSource("queueit", {})
        ctx = SourceContext(state=state, user_agent="test/1.0")

        # If the gate wrongly opens the stub will assert; if it closes
        # we return [] without touching the stub.
        events = src.fetch(ctx)

        assert events == []
        # No cooldown side-effects — the gate is a soft skip.
        assert not state.is_in_cooldown("queueit", T0 + timedelta(seconds=1))

    def test_gate_open_with_recent_signal_probes(self, stub_curl_cffi):
        state = State()
        state.record_signal(
            "reddit", "queue", "pokemoncenter",
            datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )
        stub_curl_cffi.response = _mk_normal_response()

        src = QueueItSource("queueit", {})
        ctx = SourceContext(state=state, user_agent="test/1.0")

        events = src.fetch(ctx)
        # Site normal => 0 events, but the gate DID let the request through.
        assert events == []

    def test_gate_stricter_min_sources(self, stub_curl_cffi):
        state = State()
        state.record_signal(
            "reddit", "queue", "pokemoncenter",
            datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )

        # gate_min_sources=2 means one source isn't enough — gate stays shut.
        src = QueueItSource("queueit", {"gate_min_sources": 2})
        ctx = SourceContext(state=state, user_agent="test/1.0")
        events = src.fetch(ctx)
        assert events == []

    def test_burst_bypasses_gate(self, stub_curl_cffi):
        state = State()
        # No signals at all, but we're in an active burst window.
        state.set_probe_burst(datetime.now(tz=timezone.utc) + timedelta(minutes=10))
        stub_curl_cffi.response = _mk_normal_response()

        src = QueueItSource("queueit", {})
        ctx = SourceContext(state=state, user_agent="test/1.0")
        events = src.fetch(ctx)

        # Bypassed gate, hit the stub, got a normal response → 0 events.
        assert events == []

    def test_gate_ignores_signals_from_wrong_kinds(self, stub_curl_cffi):
        state = State()
        state.record_signal(
            "reddit", "deal", "pokemoncenter",  # 'deal' isn't a signal kind
            datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )

        src = QueueItSource("queueit", {})
        ctx = SourceContext(state=state, user_agent="test/1.0")
        events = src.fetch(ctx)
        assert events == []

    def test_queue_detection_produces_confirmation_event(self, stub_curl_cffi):
        state = State()
        state.record_signal(
            "reddit", "queue", "pokemoncenter",
            datetime.now(tz=timezone.utc) - timedelta(minutes=2),
        )
        stub_curl_cffi.response = _mk_queue_response()

        src = QueueItSource("queueit", {})
        ctx = SourceContext(state=state, user_agent="test/1.0")
        events = src.fetch(ctx)

        assert len(events) == 1
        assert events[0].source == "queueit"
        assert events[0].kind == "queue"
        assert events[0].retailer == "pokemoncenter"


# ---------------------------------------------------------------------------
# Burst mode opens on confirmation (dispatcher side-effect)
# ---------------------------------------------------------------------------


class TestBurstOpensOnConfirmation:
    def test_confirmation_sets_probe_burst(self):
        state = State()
        assert not state.is_in_probe_burst(T0)

        queueit_event = DropEvent(
            source="queueit",
            title="pokemoncenter queue live",
            url="https://www.pokemoncenter.com/",
            detected_at=T0,
            kind="queue",
            region="US",
            retailer="pokemoncenter",
        )

        # No subscribers on purpose — we're only testing the side-effect.
        dispatch([queueit_event], subscribers=[], now=T0, tz_name="UTC", state=state)

        assert state.is_in_probe_burst(T0), "confirmation must open probe burst"
        # Duration matches PROBE_BURST_DURATION exactly.
        expected = (T0 + PROBE_BURST_DURATION).isoformat()
        assert state.probe_burst_until == expected

    def test_burst_constant_matches_source_default(self):
        """The dispatcher's PROBE_BURST_DURATION should equal the queueit
        source's advertised default so the docstrings don't lie."""
        assert PROBE_BURST_DURATION == timedelta(minutes=DEFAULT_BURST_MINUTES)


# ---------------------------------------------------------------------------
# Reddit multi-sub URL construction
# ---------------------------------------------------------------------------


class TestRedditMultiSub:
    def test_combined_url_used_for_multiple_subs(self):
        from pkmn_alert.sources.reddit import RedditSource
        from unittest.mock import patch, MagicMock
        import httpx

        state = State()
        src = RedditSource(
            "reddit",
            {"subreddits": ["a", "b", "c", "d"], "keyword_hints": []},
        )
        ctx = SourceContext(state=state, user_agent="test/1.0")

        empty_atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.content = empty_atom
        response.text = empty_atom.decode()

        called_urls: list[str] = []

        def spy_get(url, **_kwargs):
            called_urls.append(url)
            return response

        with patch("pkmn_alert.sources.reddit.httpx.get", side_effect=spy_get):
            src.fetch(ctx)

        assert len(called_urls) == 1, "multi-sub feed must use a single combined request"
        assert "/r/a+b+c+d/new.rss" in called_urls[0]
