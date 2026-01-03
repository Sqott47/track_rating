"""Best-effort webhook notifier to a local Twitch chat bot.

The Twitch bot runs as a separate process (even if on the same server).
This module sends a webhook to that bot when the active track changes.

This code MUST NOT raise: track activation should continue even if the bot is down.
"""

from __future__ import annotations

import json
import os
import urllib.request


def _strip_trailing_slash(value: str) -> str:
    value = (value or "").strip()
    while value.endswith("/") and value != "/":
        value = value[:-1]
    return value


def build_rate_url(track_external_url: str) -> str:
    """Return a link to the public track page scrolled to reviews."""
    url = (track_external_url or "").strip()
    if not url:
        return ""
    # Keep existing anchor if present; otherwise add #reviews.
    if "#" in url:
        return url
    return url + "#reviews"


def notify_twitch_bot_track_changed(
    *,
    channel: str | None,
    track_id: int,
    track_name: str,
    track_url_external: str,
) -> None:
    """Best-effort webhook call. Never raises."""

    webhook_url = (os.getenv("TWITCH_BOT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return

    secret = (os.getenv("TWITCH_BOT_WEBHOOK_SECRET") or "").strip()
    default_channel = (os.getenv("TWITCH_NOTIFY_CHANNEL") or "").strip()

    # Optionally force a canonical domain (useful behind reverse proxies).
    base = _strip_trailing_slash(os.getenv("PUBLIC_BASE_URL") or "")
    track_url = (track_url_external or "").strip()
    if base and track_url:
        # Replace scheme+host with PUBLIC_BASE_URL, keep path.
        try:
            parts = track_url.split("/", 3)
            if len(parts) >= 4:
                track_url = base + "/" + parts[3]
        except Exception:
            pass

    payload = {
        "channel": (channel or default_channel or "").lstrip("#"),
        "track_id": int(track_id),
        "track_name": track_name or "",
        "rate_url": build_rate_url(track_url),
    }

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if secret:
        headers["X-Webhook-Secret"] = secret

    req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2.5):
            return
    except Exception:
        return
