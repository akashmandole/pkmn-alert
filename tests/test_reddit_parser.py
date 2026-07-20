from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import httpx
import pytest

from pkmn_alert.sources.base import SourceContext
from pkmn_alert.sources.reddit import (
    RedditSource,
    _infer_kind,
    _infer_retailer,
    _is_noise,
)
from pkmn_alert.state import State

from .conftest import fixture_bytes


def _mock_get(status: int, body: bytes) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore")
    return resp


class TestRedditRSSParsing:
    def test_all_five_entries_produce_events_when_no_keyword_filter(self):
        src = RedditSource("reddit", {"subreddits": ["PokemonRestocks"], "keyword_hints": []})
        ctx = SourceContext(state=State(), user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(200, fixture_bytes("reddit_pokemonrestocks.rss")),
        ):
            events = src.fetch(ctx)

        assert len(events) == 5
        assert {e.title for e in events} == {
            "QUEUE IS LIVE on Pokemon Center — Prismatic Evolutions ETB restock!!",
            "Target has Scarlet & Violet 151 booster bundles back in stock",
            "Pokemon Center — Surging Sparks preorders now up",
            "Off-topic: what's your favorite card art?",
            "Best Buy dropped Charizard ex Premium Collection",
        }

    def test_kind_and_retailer_inferred(self):
        src = RedditSource("reddit", {"subreddits": ["PokemonRestocks"], "keyword_hints": []})
        ctx = SourceContext(state=State(), user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(200, fixture_bytes("reddit_pokemonrestocks.rss")),
        ):
            events = src.fetch(ctx)

        by_title = {e.title: e for e in events}

        queue_ev = by_title["QUEUE IS LIVE on Pokemon Center — Prismatic Evolutions ETB restock!!"]
        assert queue_ev.kind == "queue"
        assert queue_ev.retailer == "pokemoncenter"

        target_ev = by_title["Target has Scarlet & Violet 151 booster bundles back in stock"]
        assert target_ev.kind == "restock"
        assert target_ev.retailer == "target"

        preorder_ev = by_title["Pokemon Center — Surging Sparks preorders now up"]
        assert preorder_ev.kind == "preorder"
        assert preorder_ev.retailer == "pokemoncenter"

        offtopic_ev = by_title["Off-topic: what's your favorite card art?"]
        assert offtopic_ev.kind == "news"
        assert offtopic_ev.retailer == "unknown"

        bestbuy_ev = by_title["Best Buy dropped Charizard ex Premium Collection"]
        assert bestbuy_ev.kind == "restock"
        assert bestbuy_ev.retailer == "bestbuy"

    def test_keyword_hints_filter_at_source(self):
        src = RedditSource(
            "reddit",
            {"subreddits": ["PokemonRestocks"], "keyword_hints": ["pokemon center"]},
        )
        ctx = SourceContext(state=State(), user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(200, fixture_bytes("reddit_pokemonrestocks.rss")),
        ):
            events = src.fetch(ctx)

        titles = {e.title for e in events}
        assert "QUEUE IS LIVE on Pokemon Center — Prismatic Evolutions ETB restock!!" in titles
        assert "Pokemon Center — Surging Sparks preorders now up" in titles
        assert "Target has Scarlet & Violet 151 booster bundles back in stock" not in titles

    def test_second_fetch_dedupes_via_last_id(self):
        state = State()
        src = RedditSource("reddit", {"subreddits": ["PokemonRestocks"], "keyword_hints": []})
        ctx = SourceContext(state=state, user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(200, fixture_bytes("reddit_pokemonrestocks.rss")),
        ):
            first = src.fetch(ctx)
            assert len(first) == 5
            # Same fixture served again — the source should notice the newest
            # id hasn't changed and return 0 events.
            second = src.fetch(ctx)

        assert second == [], "second fetch of unchanged feed must produce 0 events"

    def test_429_triggers_cooldown_and_returns_empty(self):
        state = State()
        src = RedditSource("reddit", {"subreddits": ["PokemonRestocks"], "keyword_hints": []})
        ctx = SourceContext(state=state, user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(429, b"Too many requests"),
        ):
            events = src.fetch(ctx)

        assert events == []
        now = datetime.now(tz=timezone.utc)
        assert state.is_in_cooldown("reddit", now), "429 must trip source cooldown"


class TestKindInferenceWordBoundaries:
    """Regression coverage for the 2026-07-20 false positive.

    A user got a ntfy push at 04:49 UTC titled
      "[MED] Pokemon Center PSA 10 Special Delivery Bidoof Sells $3500 ..."
    because the previous classifier did substring matching and ``"live" in
    "delivery"`` is True. These tests pin word-boundary behavior so the
    bug (and its siblings) can't reappear."""

    @pytest.mark.parametrize("title", [
        "Pokemon Center PSA 10 Special Delivery Bidoof Sells $3500 Promotion by eBay!",
        "Special Delivery Charizard graded PSA 10",
        "Alive and well: my collection update",
        "Livestream tonight — opening booster boxes",
        "Olive Green promo card giveaway",
    ])
    def test_live_substring_does_not_trigger_restock(self, title):
        assert _infer_kind(title) != "restock", (
            f"'live' matched as substring in {title!r}; should require word boundary"
        )

    @pytest.mark.parametrize("title", [
        "Eyedrops for dry eyes (off topic)",
        "Airdrop teased for next set",
        "New raindrops promo card leaked",
    ])
    def test_drop_substring_does_not_trigger_restock(self, title):
        assert _infer_kind(title) != "restock"

    @pytest.mark.parametrize("title,expected", [
        ("QUEUE IS LIVE on Pokemon Center", "queue"),
        ("Pokemon Center waiting room active", "queue"),
        ("Pokemon Center — Surging Sparks preorders now up", "preorder"),
        ("Target has 151 back in stock", "restock"),
        ("Best Buy dropped Charizard ex Premium Collection", "restock"),
        ("Prismatic Evolutions ETB now live at Pokemon Center", "restock"),
        ("Off-topic: what's your favorite card art?", "news"),
        ("Pokemon Center PSA 10 Special Delivery Bidoof Sells $3500 Promotion by eBay!", "news"),
    ])
    def test_kind_inference_matrix(self, title, expected):
        assert _infer_kind(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Target has 151 booster bundles", "target"),
        ("Now targeting a full collection", "unknown"),   # \btarget\b, not "targeting"
        ("Best Buy dropped Charizard", "bestbuy"),
        ("Amazon has Mega Moonlit Tin", "amazon"),
        ("Amazonian rainforest documentary", "unknown"),  # \bamazon\b, not "amazonian"
        ("Pokemon Center — Surging Sparks", "pokemoncenter"),
        ("Pokémon Center EU restock", "pokemoncenter"),
    ])
    def test_retailer_inference_matrix(self, title, expected):
        assert _infer_retailer(title) == expected


class TestNoiseBlocklist:
    """Secondary-market and analytics chatter that mentions Pokemon Center
    but is NOT actionable. These titles trigger _is_noise() and get
    downgraded to news+unknown at the source, so subscriber filters
    exclude them."""

    @pytest.mark.parametrize("title", [
        "Pokemon Center PSA 10 Special Delivery Bidoof Sells $3500 Promotion by eBay!",
        "Evolving Skies PSA 10 Rayquaza Vmax Sells $2850 Promotion by eBay!",
        "Phantasmal Flames Sealed Booster Boxes $408 Promotion by eBay!",
        "Charizard PSA 10 sold for $12,000 on eBay",
        "Paldean Fates Bubble Mew Up 187% Past Year",
        "Prismatic Evolutions down 12% past month",
    ])
    def test_flagged_as_noise(self, title):
        assert _is_noise(title), f"expected noise flag on: {title!r}"

    @pytest.mark.parametrize("title", [
        "QUEUE IS LIVE on Pokemon Center — Prismatic Evolutions ETB restock!!",
        "Pokemon Center — Surging Sparks preorders now up",
        "Target has 151 booster bundles back in stock",
    ])
    def test_real_drops_not_flagged(self, title):
        assert not _is_noise(title), f"real drop should not be noise: {title!r}"

    def test_noisy_pc_mention_dispatched_as_news_unknown(self):
        """End-to-end: the exact false-positive title flows through the
        RedditSource and emerges as kind=news, retailer=unknown — which
        the `me` subscriber's filter rejects."""
        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_bidoof_false_positive</id>
    <title>Pokemon Center PSA 10 Special Delivery Bidoof Sells $3500 Promotion by eBay!</title>
    <link href="https://www.reddit.com/r/PokeInvesting/comments/xxx/"/>
    <updated>2026-07-20T04:45:00Z</updated>
    <category term="r/PokeInvesting"/>
  </entry>
</feed>"""
        src = RedditSource("reddit", {"subreddits": ["PokeInvesting"], "keyword_hints": []})
        ctx = SourceContext(state=State(), user_agent="test-agent/1.0")

        with patch(
            "pkmn_alert.sources.reddit.httpx.get",
            return_value=_mock_get(200, rss),
        ):
            events = src.fetch(ctx)

        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "news", (
            "eBay sale post should classify as news, not restock — "
            "was the substring bug reintroduced?"
        )
        assert ev.retailer == "unknown", (
            "noise-flagged posts must not claim retailer=pokemoncenter"
        )
