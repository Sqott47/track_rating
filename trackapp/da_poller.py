"""DonationAlerts poller.

Runs in a loop, reads recent donations via DonationAlerts API and matches them to
pending TrackSubmission.payment_ref (code in comment). Intended to be started via systemd.

Usage:
    python -m trackapp.da_poller
"""

import os
import re
import time
from datetime import datetime, timedelta

from . import app, db
from .models import TrackSubmission
from .donationalerts import get_valid_access_token, list_donations, load_tokens, save_tokens

TG_BOT_TOKEN = os.getenv("TRACKRATER_TG_BOT_TOKEN", "").strip()

CURRENCY_ACCEPT = os.getenv("DA_ACCEPT_CURRENCY", "RUB").strip().upper() or "RUB"
POLL_INTERVAL = int(os.getenv("DA_POLL_INTERVAL", "7"))  # seconds
MAX_AGE_MIN = int(os.getenv("DA_PENDING_MAX_AGE_MIN", "20"))


def _norm_currency(cur: str) -> str:
    cur = (cur or "").strip().upper()
    if cur in ("RUR", "₽"):
        return "RUB"
    return cur


def _get_pending():
    # We consider ANY submission with a pending DA payment reference, regardless of current status.
    # This allows "raise priority" flow, where a track stays queued while payment is pending.
    return (
        TrackSubmission.query
        .filter(TrackSubmission.payment_status == "pending")
        .filter(TrackSubmission.payment_provider == "donationalerts")
        .all()
    )


def _notify_tg(chat_id: int, text: str) -> None:
    if not TG_BOT_TOKEN or not chat_id:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _match_and_apply(donation: dict, pending: list[TrackSubmission]) -> int:
    msg = str(donation.get("message") or "")
    amount = donation.get("amount")
    cur = _norm_currency(str(donation.get("currency") or ""))
    if cur != CURRENCY_ACCEPT:
        return 0
    try:
        amount_i = int(float(amount))
    except Exception:
        return 0

    hits = 0
    for sub in pending:
        code = (sub.payment_ref or "").strip()
        if not code:
            continue
        if code in msg:
            provider_ref = f"da:{donation.get('id')}"
            # idempotency: if already paid with same ref - skip
            if sub.payment_status == "paid":
                continue
            # amount check
            required = int(sub.payment_amount or sub.priority or 0)
            if amount_i < required:
                continue
            # Finalize storage only for new submissions not enqueued yet
            # Ensure raw file is finalized to storage/S3 for paid submissions.
            # We try regardless of current status; if tmp already gone, ignore FileNotFoundError.
            try:
                from .routes import _finalize_tmp_to_storage
                _finalize_tmp_to_storage(sub)
            except FileNotFoundError:
                # Already finalized or tmp cleaned up
                pass
            except Exception:
                app.logger.exception("[DA poller] finalize failed for submission_id=%s file_uuid=%s", sub.id, sub.file_uuid)
            # Apply priority only on successful payment
            sub.priority = required
            sub.priority_set_at = datetime.utcnow()
            if (sub.status or "") != "playing":
                sub.status = "queued"
            sub.payment_status = "paid"
            sub.payment_provider = "donationalerts"
            sub.payment_ref = provider_ref
            sub.payment_amount = required
            db.session.add(sub)
            hits += 1
            _notify_tg(
                int(sub.tg_user_id or 0),
                f"✅ Оплата получена ({required} RUB). Трек добавлен в очередь!",
            )
    if hits:
        db.session.commit()
        try:
            from .routes import _broadcast_queue_state
            _broadcast_queue_state()
        except Exception:
            app.logger.exception("[DA poller] broadcast queue state failed")
    return hits


def main():
    print(f"[DA poller] start, interval={POLL_INTERVAL}s, accept_currency={CURRENCY_ACCEPT}")
    while True:
        try:
            with app.app_context():
                tokens = load_tokens()
                last_id = int(tokens.get("last_donation_id") or 0)

                access = get_valid_access_token()
                data = list_donations(access, page=1) or {}
                donations = (data.get("data") or [])
                # Sort by id ascending
                donations_sorted = sorted(donations, key=lambda d: int(d.get("id") or 0))

                pending = _get_pending()
                if not pending:
                    # still advance last_id to avoid reprocessing huge history
                    if donations_sorted:
                        last_id = max(last_id, int(donations_sorted[-1].get("id") or 0))
                        tokens["last_donation_id"] = last_id
                        save_tokens(tokens)
                    time.sleep(POLL_INTERVAL)
                    continue

                max_seen = last_id
                total_hits = 0
                for d in donations_sorted:
                    did = int(d.get("id") or 0)
                    if did <= last_id:
                        continue
                    max_seen = max(max_seen, did)
                    total_hits += _match_and_apply(d, pending)

                if max_seen != last_id:
                    tokens["last_donation_id"] = max_seen
                    save_tokens(tokens)

                if total_hits:
                    print(f"[DA poller] matched {total_hits} payments")

        except Exception as e:
            print(f"[DA poller] error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
