"""Notifiers — one per delivery channel."""

from __future__ import annotations

import logging
from typing import Any

from ..config import ChannelConfig
from .base import Notifier

log = logging.getLogger(__name__)


def build(channel: ChannelConfig) -> Notifier | None:
    if channel.type == "ntfy":
        from .ntfy import NtfyNotifier
        return NtfyNotifier(channel.options)
    if channel.type == "email":
        from .email_smtp import EmailNotifier
        return EmailNotifier(channel.options)
    if channel.type == "sms_gateway":
        from .sms_gateway import SmsGatewayNotifier
        return SmsGatewayNotifier(channel.options)
    if channel.type == "webhook":
        from .webhook import WebhookNotifier
        return WebhookNotifier(channel.options)
    if channel.type == "macos":
        from .macos import MacOSNotifier
        return MacOSNotifier(channel.options)
    if channel.type == "stdout":
        from .stdout import StdoutNotifier
        return StdoutNotifier(channel.options)

    log.warning("unknown channel type %r; skipping", channel.type)
    return None


__all__ = ["Notifier", "build"]
