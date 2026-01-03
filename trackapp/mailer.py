import os
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests
from flask import current_app


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def generate_token() -> str:
    # URL-safe token suitable for query params
    return secrets.token_urlsafe(32)


def resend_send_email(*, to_email: str, subject: str, html: str, text: Optional[str] = None) -> Tuple[bool, str]:
    """Send email via Resend.

    Returns (ok, message). Message is either an error description or the Resend id.
    """
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return False, "RESEND_API_KEY is not set"

    # While the domain isn't verified, you can keep using Resend's shared domain:
    # TrackRater <onboarding@resend.dev>
    from_email = os.getenv("RESEND_FROM", "TrackRater <onboarding@resend.dev>").strip()

    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if r.status_code >= 200 and r.status_code < 300:
            data = r.json() if r.content else {}
            return True, str(data.get("id", "sent"))
        return False, f"{r.status_code}: {r.text}"
    except Exception as e:
        current_app.logger.exception("Resend send failed")
        return False, str(e)
