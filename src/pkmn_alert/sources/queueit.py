"""Direct Queue-it detector — opt-in, HTTP-only.

# Why this exists

The other sources (reddit, nitter, rss) are *derivative* — they only fire
after some human or bot on the outside has noticed the drop and posted
about it. This source is the primary observer: it hits pokemoncenter.com
itself and looks for Queue-it signals in the response.

# Why it's opt-in

Pokemon Center's Terms of Use prohibit automated access. Even at 1 request
per 5 minutes we are technically in violation. The risk of getting our
GitHub Actions IP range put on a bad-reputation list is real but small. We
default this OFF and leave it to the user to turn on knowingly.

# What we do NOT do

- We do NOT try to bypass the queue.
- We do NOT try to reserve a queue position.
- We do NOT execute Queue-it's JavaScript proof-of-work.
- We do NOT do this from a residential proxy pool.
- We do NOT poll faster than the cron interval (5 min).

We just observe whether the front page is in normal-shopping mode or in
waiting-room mode, and forward that observation to the notifier.

# How we look like a real client at the TLS layer

Standard `requests`/`httpx` have a distinctive JA3/JA4 TLS fingerprint that
Akamai (which fronts pokemoncenter.com) can identify at the handshake
before HTTP headers are ever read. `curl_cffi` links against a patched
curl that reproduces Chrome's TLS ClientHello and HTTP/2 SETTINGS frame,
which makes us indistinguishable from a real Chrome at the network layer.

We still can't produce a real `_abck` cookie — that requires executing
Akamai's `sensor.js` in a real browser — but for a one-shot page fetch
that only cares about the initial redirect / cookie set, we don't need one.

# Detection signals

1. HTTP redirect: response URL contains `queue-it.net` or `waitingroom`.
2. Response body contains any of the QUEUE_TEXT_SIGNALS.
3. Response sets a `QueueITAccepted` / `QueueITUnlockLink` cookie.

Any single signal is enough to fire. False positives on the front page
are essentially zero — the site NEVER shows queue markup in normal state.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..event import DropEvent
from .base import Source, SourceContext

log = logging.getLogger(__name__)

DEFAULT_URL = "https://www.pokemoncenter.com/"

# Text markers we look for in the response body. All lowercase; we match
# case-insensitively. Sourced from Queue-it's public queue templates.
QUEUE_TEXT_SIGNALS = (
    "you're in the virtual queue",
    "youre in the virtual queue",
    "hi, trainer! you're in the virtual queue",
    "virtual queue to access pokemon center",
    "virtual queue to access pokémon center",
    "estimated wait time",
    "waiting room",
    "queue-it.net",
    "queueit",
    "keep this window open to stay in the queue",
)

QUEUE_COOKIE_NAMES = ("QueueITAccepted", "QueueITUnlockLink", "QueueITAccepted-SDFrts345E-V3")

# Big enough to catch queue markup at the top of the page but small enough
# that we're not slurping the entire ~1MB SPA bundle on every check.
MAX_BODY_BYTES = 200_000

# When we get blocked (403, 412, 429), stay quiet for this long before
# trying again. Prevents cascading rate-limit noise.
COOLDOWN_ON_BLOCK = timedelta(minutes=30)


class QueueItSource(Source):
    def fetch(self, ctx: SourceContext) -> list[DropEvent]:
        # Respect our own cooldown after a previous block.
        now = datetime.now(tz=timezone.utc)
        if ctx.state.is_in_cooldown(self.id, now):
            log.info("[%s] in cooldown; skipping this tick", self.id)
            return []

        url: str = self.options.get("url", DEFAULT_URL)
        # Randomize the ClientHello a bit across runs so we don't paint the
        # same JA3 on every request — real users vary by minor Chrome version.
        # curl_cffi 0.7.x supports a fixed list of profiles. chrome131 was
        # added in 0.8+ and errors out on 0.7.4 with "Impersonating chromeXXX
        # is not supported". Default to the newest profiles that 0.7.x
        # actually ships. Users can override via sources.yaml if they pin
        # a newer curl_cffi.
        impersonate_choices = self.options.get(
            "impersonate_choices", ["chrome124", "chrome120", "chrome116"]
        )
        impersonate = random.choice(impersonate_choices)

        try:
            # Lazy import so the base install (without curl_cffi) still works.
            from curl_cffi import requests as curl_requests
        except ImportError:
            log.error(
                "[%s] curl_cffi is not installed. This source requires it. "
                "Install with: pip install -r requirements-queueit.txt",
                self.id,
            )
            # Long cooldown to avoid spamming the error every tick.
            ctx.state.set_cooldown(self.id, now + timedelta(hours=6))
            return []

        try:
            resp = curl_requests.get(
                url,
                impersonate=impersonate,
                timeout=ctx.timeout_s,
                allow_redirects=True,
                headers={
                    # curl_cffi already sends Chrome-like default headers when
                    # impersonating; we override the language to match a US
                    # visitor. Do NOT override User-Agent — that would create
                    # a Chrome-TLS + non-Chrome-UA mismatch which is a strong
                    # bot signal.
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                },
            )
        except Exception as exc:  # curl_cffi raises curl_cffi.CurlError; broad catch keeps us robust
            log.warning("[%s] request to %s failed: %s", self.id, url, exc)
            ctx.state.set_cooldown(self.id, now + COOLDOWN_ON_BLOCK)
            return []

        # A block from Akamai looks like 403/412; from Queue-it/CDN itself 429.
        if resp.status_code in (403, 412, 429):
            log.warning(
                "[%s] pokemoncenter.com blocked us with HTTP %s; cooling down %s",
                self.id, resp.status_code, COOLDOWN_ON_BLOCK,
            )
            ctx.state.set_cooldown(self.id, now + COOLDOWN_ON_BLOCK)
            return []

        # A redirect to a queue-it.net URL is the strongest possible signal.
        final_url = str(getattr(resp, "url", url)).lower()
        redirect_hit = "queue-it.net" in final_url or "waitingroom" in final_url

        cookies = _cookies_lower(resp)
        cookie_hit = any(name.lower() in cookies for name in QUEUE_COOKIE_NAMES)

        body = _short_body(resp).lower()
        text_hits = [sig for sig in QUEUE_TEXT_SIGNALS if sig in body]

        signals_fired = {
            "redirect": redirect_hit,
            "cookie": cookie_hit,
            "text": bool(text_hits),
        }

        if not any(signals_fired.values()):
            log.info("[%s] site normal (impersonate=%s status=%s)", self.id, impersonate, resp.status_code)
            return []

        confidence = _confidence(signals_fired, text_hits)
        title = _build_title(signals_fired, text_hits, final_url)
        log.info(
            "[%s] queue signals fired=%s confidence=%.2f status=%s final_url=%s",
            self.id, signals_fired, confidence, resp.status_code, final_url,
        )

        return [
            DropEvent(
                source=self.id,
                title=title,
                url="https://www.pokemoncenter.com/",
                detected_at=now,
                kind="queue",
                region="US",
                retailer="pokemoncenter",
                confidence=confidence,
                raw={
                    "signals": signals_fired,
                    "text_hits": text_hits,
                    "final_url": final_url,
                    "status": resp.status_code,
                    "impersonate": impersonate,
                },
            )
        ]


def _short_body(resp: Any) -> str:
    try:
        content = resp.content or b""
    except Exception:
        return ""
    return content[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")


def _cookies_lower(resp: Any) -> set[str]:
    out: set[str] = set()
    try:
        for cookie in resp.cookies:
            # curl_cffi cookies expose .name on each entry
            name = getattr(cookie, "name", None)
            if name:
                out.add(name.lower())
    except Exception:
        pass
    return out


def _confidence(signals: dict[str, bool], text_hits: list[str]) -> float:
    # Redirect alone is 100% confidence — the site literally sent us to
    # queue-it.net. Cookie alone is also decisive. Text signals are strong
    # but each individual keyword is a weaker hint.
    if signals["redirect"] or signals["cookie"]:
        return 1.0
    if len(text_hits) >= 2:
        return 0.95
    return 0.7


def _build_title(signals: dict[str, bool], text_hits: list[str], final_url: str) -> str:
    if signals["redirect"]:
        return f"Pokemon Center: QUEUE LIVE (redirected to {final_url[:80]})"
    if signals["cookie"]:
        return "Pokemon Center: QUEUE LIVE (queue-it cookie set)"
    hit = text_hits[0] if text_hits else "queue markup"
    return f"Pokemon Center: QUEUE LIKELY LIVE (matched '{hit}')"
