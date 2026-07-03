"""CLI entrypoint.

Designed for the "run-once, exit" model that pairs with cron / GitHub
Actions. There's no background loop — each cron tick spawns a fresh
process, reads state.json, does its work, writes state.json, and exits.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config as cfgmod, state as statemod
from .dispatch import dispatch
from .sources import build as build_source

log = logging.getLogger("pkmn_alert")


DEFAULT_USER_AGENT = (
    "pkmn-alert/0.1 (+https://github.com/example/pokemon-drop-alert) "
    "python-httpx/0.27 for personal restock notifications"
)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pkmn-alert",
        description="Aggregate Pokemon TCG drop signals from free sources and notify subscribers.",
    )
    p.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    p.add_argument("--subscribers", type=Path, default=Path("subscribers.yaml"))
    p.add_argument("--state", type=Path, default=Path("state.json"))
    p.add_argument(
        "--timezone",
        default="America/Los_Angeles",
        help="IANA timezone name used to evaluate quiet_hours (default: America/Los_Angeles).",
    )
    p.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent sent to all sources. Change the URL to point at your fork.",
    )
    p.add_argument(
        "--jitter-max-s",
        type=float,
        default=0.0,
        help=(
            "Sleep a random 0..N seconds before fetching. Recommended: 60 in "
            "GitHub Actions so we don't hit sources at exactly :00/:05/:10."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and dedupe, but do NOT actually send notifications. Logs what would go out.",
    )
    p.add_argument(
        "--seed-only",
        action="store_true",
        help=(
            "Fetch, mark everything as already-seen, but send nothing. Use on "
            "first-ever run so you don't get a flood of historical alerts."
        ),
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for INFO, -vv for DEBUG.")
    p.add_argument("--version", action="version", version=f"pkmn-alert {__version__}")
    return p


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    _setup_logging(args.verbose)

    if args.jitter_max_s > 0:
        wait = random.uniform(0, args.jitter_max_s)
        log.info("jittering %.1fs before first fetch", wait)
        time.sleep(wait)

    cfg = cfgmod.load(
        sources_path=args.sources,
        subscribers_path=args.subscribers,
        state_path=args.state,
        timezone_name=args.timezone,
    )
    log.info(
        "loaded config: sources=%d subscribers=%d tz=%s",
        len(cfg.sources), len(cfg.subscribers), cfg.timezone,
    )

    state = statemod.load(cfg.state_path)
    now = datetime.now(tz=timezone.utc)

    # ---- fetch ----
    all_events = []
    from .sources.base import SourceContext
    ctx = SourceContext(state=state, user_agent=args.user_agent)

    for scfg in cfg.sources:
        if not scfg.enabled:
            log.debug("source %s disabled; skipping", scfg.id)
            continue
        if state.is_in_cooldown(scfg.id, now):
            log.info("source %s in cooldown; skipping", scfg.id)
            continue
        source = build_source(scfg)
        if source is None:
            continue
        try:
            events = source.fetch(ctx)
        except Exception:
            # Very last line of defense: a source raising unexpectedly
            # should never crash the run. Log with traceback and move on.
            log.exception("source %s raised unexpectedly", scfg.id)
            continue
        log.info("source %s produced %d events", scfg.id, len(events))
        all_events.extend(events)

    # ---- dedupe ----
    fresh = []
    for event in all_events:
        key = event.dedupe_key()
        if state.has_seen(key):
            log.debug("dedupe hit for %s (%s)", key, event.title[:60])
            continue
        state.mark_seen(key, now)
        fresh.append(event)

    log.info("fetched=%d fresh=%d", len(all_events), len(fresh))

    # ---- seed-only short circuit ----
    if args.seed_only:
        log.warning("--seed-only: not dispatching; %d events marked as seen", len(fresh))
        state.prune(now)
        statemod.save(cfg.state_path, state)
        return 0

    # ---- dispatch ----
    result = dispatch(
        fresh,
        cfg.subscribers,
        now=now,
        tz_name=cfg.timezone,
        dry_run=args.dry_run,
    )
    log.info(
        "dispatch complete: delivered=%d filtered=%d unconfigured=%d failed=%d dry_run=%s",
        result.delivered,
        result.skipped_filtered,
        result.skipped_unconfigured,
        result.failed,
        args.dry_run,
    )

    # ---- persist ----
    state.prune(now)
    statemod.save(cfg.state_path, state)

    # Exit non-zero only on hard config problems, never on transient send
    # failures — we want the GH Actions run to stay green so the scheduler
    # keeps ticking.
    return 0


if __name__ == "__main__":
    sys.exit(main())
