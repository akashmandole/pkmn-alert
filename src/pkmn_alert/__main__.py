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
from .dispatch import dispatch, process_due_reminders
from .sources import build as build_source

log = logging.getLogger("pkmn_alert")


DEFAULT_USER_AGENT = (
    "pkmn-alert/0.1 (+https://github.com/example/pokemon-drop-alert) "
    "python-httpx/0.27 for personal restock notifications"
)

# Kept in sync with sources/queueit.py's GATE_SIGNAL_KINDS/RETAILERS.
# The queueit gate reads these same values via its options block; we
# only apply them here to decide what qualifies as a "signal" worth
# writing to state.signal_log in the first place.
_SIGNAL_KINDS = {"queue", "restock", "preorder"}
_SIGNAL_RETAILERS = {"pokemoncenter", "unknown"}


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

    # ---- fire due reminders BEFORE fetching ----
    #
    # We do this first so a slow / rate-limited fetch later in the tick
    # never delays a due reminder past its intended time. The reminder
    # payloads live in state.json and were queued by prior successful
    # dispatches; the notifier configs are frozen at queue time so
    # mid-flight subscriber edits don't corrupt in-flight reminders.
    if not args.seed_only:
        fired = process_due_reminders(
            state, cfg.subscribers, now=now, tz_name=cfg.timezone, dry_run=args.dry_run,
        )
        if fired:
            log.info("fired %d due reminder(s) before fetch", fired)

    # ---- fetch ----
    #
    # We fetch each source in turn and dedupe its output IMMEDIATELY, so
    # that the signal_log used by downstream sources' confidence gates
    # already reflects this tick's fresh events. Sources are ordered so
    # that any source with a gate check (currently just queueit) runs
    # LAST — this way it sees Reddit et al's fresh events from this same
    # tick and can decide whether to spend its request budget.
    from .sources.base import SourceContext
    ctx = SourceContext(state=state, user_agent=args.user_agent)

    # queueit-type sources sort to the end. Any future gate-checking
    # source can flip a `runs_after_signals` attribute or use the same
    # type name to opt into "run last".
    def _source_order(scfg):
        return 1 if scfg.type == "queueit" else 0

    sources_ordered = sorted(cfg.sources, key=_source_order)

    all_events: list = []
    fresh: list = []
    total_fetched = 0

    for scfg in sources_ordered:
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
        total_fetched += len(events)
        all_events.extend(events)

        # Per-source dedupe + signal-log update. Doing this inline (rather
        # than in a separate pass after the loop) is what lets downstream
        # sources' gate checks see this tick's fresh events.
        for event in events:
            key = event.dedupe_key()
            if state.has_seen(key):
                log.debug("dedupe hit for %s (%s)", key, event.title[:60])
                continue
            state.mark_seen(key, now)
            fresh.append(event)

            # Record a signal for the confidence gate. We only credit
            # derivative sources — the queueit source doesn't get to
            # justify its own next probe — and only kinds/retailers we
            # care about so random deal chatter doesn't burn our budget.
            if scfg.type != "queueit" and event.kind in _SIGNAL_KINDS \
                    and event.retailer in _SIGNAL_RETAILERS:
                state.record_signal(
                    scfg.id, event.kind, event.retailer, now,
                )

    log.info("fetched=%d fresh=%d", total_fetched, len(fresh))

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
        state=state,
        dry_run=args.dry_run,
    )
    log.info(
        "dispatch complete: delivered=%d reminders_queued=%d confirmations=%d "
        "filtered=%d unconfigured=%d awaiting_confirmation=%d coalesced=%d "
        "suppressed=%d failed=%d dry_run=%s",
        result.delivered,
        result.reminders_queued,
        result.confirmations_recorded,
        result.skipped_filtered,
        result.skipped_unconfigured,
        result.skipped_awaiting_confirmation,
        result.coalesced,
        result.suppressed_recent_drop,
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
