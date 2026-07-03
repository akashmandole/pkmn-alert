"""Signal sources — each one turns some external feed into ``DropEvent``s."""

from __future__ import annotations

import logging
from typing import Any

from ..config import SourceConfig
from .base import Source

log = logging.getLogger(__name__)


def build(cfg: SourceConfig) -> Source | None:
    """Factory that resolves a config entry into a concrete Source.

    Returns None (and logs) for unknown types so a typo in ``sources.yaml``
    doesn't crash the whole run.
    """
    # Local imports keep optional deps (curl_cffi) from loading unless the
    # user actually enabled that source.
    if cfg.type == "reddit":
        from .reddit import RedditSource
        return RedditSource(cfg.id, cfg.options)
    if cfg.type == "rss":
        from .rss import RSSSource
        return RSSSource(cfg.id, cfg.options)
    if cfg.type == "nitter":
        from .nitter import NitterSource
        return NitterSource(cfg.id, cfg.options)
    if cfg.type == "queueit":
        try:
            from .queueit import QueueItSource
        except ImportError:
            log.error(
                "source '%s' requires curl_cffi. Install with: "
                "pip install -r requirements-queueit.txt",
                cfg.id,
            )
            return None
        return QueueItSource(cfg.id, cfg.options)

    log.warning("unknown source type %r for id %r; skipping", cfg.type, cfg.id)
    return None


__all__ = ["Source", "build"]
