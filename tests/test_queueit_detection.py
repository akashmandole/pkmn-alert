"""Verify the queue-it detection logic against saved HTML fixtures.

We can't easily test the ``curl_cffi`` fetch itself (requires the optional
dependency), so we test the *detection* logic — the pure text/cookie/URL
matching that decides whether a given response is a queue page.
"""

from pkmn_alert.sources.queueit import QUEUE_TEXT_SIGNALS

from .conftest import fixture_text


def _text_hits(body: str) -> list[str]:
    b = body.lower()
    return [sig for sig in QUEUE_TEXT_SIGNALS if sig in b]


class TestQueueTextDetection:
    def test_queue_page_fires_multiple_signals(self):
        hits = _text_hits(fixture_text("pokemoncenter_queue.html"))
        # The queue fixture contains the primary phrase, the wait-time
        # phrase, and the queue-it script src. Any single one is enough
        # for a live alert; multiple hits raise our confidence.
        assert len(hits) >= 3, f"expected multiple signals, got only: {hits}"
        assert any("virtual queue" in h for h in hits)

    def test_normal_homepage_fires_no_signals(self):
        assert _text_hits(fixture_text("pokemoncenter_normal.html")) == []

    def test_partial_match_still_catches_queue(self):
        # A stripped-down queue response where all we get is the wait-time
        # banner (e.g. a Queue-it iframe embed) should still fire.
        body = "<html><body>Estimated wait time: 03:15:22</body></html>"
        assert "estimated wait time" in _text_hits(body)
