from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import httpx

from pkmn_alert.sources.base import SourceContext
from pkmn_alert.sources.reddit import RedditSource
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
