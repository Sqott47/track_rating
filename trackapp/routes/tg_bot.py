"""Telegram Bot private API routes.

Migrated from the monolithic routes.py for better maintainability.
"""

import os
import shutil
from datetime import datetime
from uuid import uuid4

import requests
from flask import request, jsonify

from ..core import (
    app, db,
    _get_s3_client,
    _submission_display_name,
)
from ..extensions import (
    ALLOWED_SUBMISSION_EXTS,
    S3_BUCKET,
    S3_KEEP_LOCAL,
    S3_PREFIX,
    SUBMISSIONS_RAW_DIR,
    SUBMISSIONS_TMP_DIR,
)
from ..models import TrackSubmission
from ..state import _broadcast_queue_state


# -----------------
# Config
# -----------------

TG_API_TOKEN = (
    os.getenv("TRACKRATER_TG_API_TOKEN")
    or os.getenv("TG_API_TOKEN")
    or ""
).strip()

_TG_BOT_TOKEN = (os.getenv("TRACKRATER_TG_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN") or "").strip()
_TG_NOTIFY_DEBUG = (os.getenv("TG_NOTIFY_DEBUG") or "").strip() == "1"


# -----------------
# Helpers
# -----------------

def _require_tg_bot_token():
    if not TG_API_TOKEN:
        return False
    got = (request.headers.get("X-Bot-Token") or "").strip()
    return got and got == TG_API_TOKEN


def _tmp_path_for(uuid_hex: str, ext: str) -> str:
    return os.path.join(SUBMISSIONS_TMP_DIR, f"{uuid_hex}.{ext}")


def _raw_key_for(uuid_hex: str, ext: str) -> str:
    """Build S3 object key for raw submission audio."""
    ext = (ext or "").lower().lstrip(".")
    prefix = (S3_PREFIX or "submissions_raw/").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{uuid_hex}.{ext}"


def _content_type_for_ext(ext: str) -> str:
    ext = (ext or "").lower()
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
        "aif": "audio/aiff",
        "aiff": "audio/aiff",
    }.get(ext, "application/octet-stream")


def _finalize_tmp_to_storage(sub: TrackSubmission) -> None:
    """Upload tmp file to S3 (if configured) and/or copy to local raw folder."""
    ext = (sub.original_ext or "").lower()
    tmp_path = _tmp_path_for(sub.file_uuid, ext)
    if not os.path.isfile(tmp_path):
        raise FileNotFoundError("tmp file missing")

    raw_filename = f"{sub.file_uuid}.{ext}"
    raw_path = os.path.join(SUBMISSIONS_RAW_DIR, raw_filename)

    s3 = _get_s3_client()
    if s3:
        key = _raw_key_for(sub.file_uuid, ext)
        with open(tmp_path, "rb") as f:
            s3.upload_fileobj(
                Fileobj=f,
                Bucket=S3_BUCKET,
                Key=key,
                ExtraArgs={"ContentType": _content_type_for_ext(ext)},
            )
        if S3_KEEP_LOCAL:
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            shutil.copyfile(tmp_path, raw_path)
    else:
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        shutil.copyfile(tmp_path, raw_path)

    try:
        os.remove(tmp_path)
    except Exception:
        pass


def _queue_position(submission_id: int) -> int:
    """Compute 1-based position in queue."""
    rows = (
        db.session.query(TrackSubmission)
        .filter(TrackSubmission.status.in_(["queued", "waiting_payment", "draft"]))
        .order_by(
            TrackSubmission.priority.desc(),
            TrackSubmission.priority_set_at.asc(),
            TrackSubmission.created_at.asc(),
            TrackSubmission.id.asc(),
        )
        .all()
    )
    for idx, s in enumerate(rows, start=1):
        if s.id == submission_id:
            return idx
    return -1


def _notify_submission_tg(sub: TrackSubmission | None, text: str) -> None:
    """Send notification to Telegram user about their submission."""
    if not _TG_BOT_TOKEN or not sub or not sub.tg_user_id:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{_TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": int(sub.tg_user_id),
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=7,
        )
        if not response.ok and _TG_NOTIFY_DEBUG:
            app.logger.warning("Telegram notify returned %s: %s", response.status_code, response.text[:200])
    except requests.Timeout:
        app.logger.warning("Telegram notify timeout for tg_user_id=%s", sub.tg_user_id)
    except requests.RequestException as e:
        app.logger.warning("Telegram notify failed for tg_user_id=%s: %s", sub.tg_user_id, str(e)[:100])
    except Exception:
        if _TG_NOTIFY_DEBUG:
            app.logger.exception("Telegram notify unexpected error")


# -----------------
# Routes
# -----------------

@app.route("/api/tg/submissions", methods=["POST"])
def tg_create_submission():
    """Create a draft submission from Telegram bot."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403

    tg_user_id = request.form.get("tg_user_id")
    tg_username = request.form.get("tg_username") or ""
    original_filename = request.form.get("original_filename") or ""
    ext = (request.form.get("original_ext") or "").lower().lstrip(".")
    file = request.files.get("file")

    if not tg_user_id or not str(tg_user_id).isdigit():
        return jsonify({"error": "tg_user_id required"}), 400
    if ext not in ALLOWED_SUBMISSION_EXTS:
        return jsonify({"error": "unsupported ext", "allowed": sorted(ALLOWED_SUBMISSION_EXTS)}), 400
    if not file:
        return jsonify({"error": "file required"}), 400

    file_uuid = uuid4().hex
    tmp_path = _tmp_path_for(file_uuid, ext)
    try:
        file.save(tmp_path)
    except Exception as e:
        print("tg tmp save failed:", e)
        return jsonify({"error": "save failed"}), 500

    sub = TrackSubmission(
        artist="",
        title="",
        priority=0,
        status="draft",
        file_uuid=file_uuid,
        original_filename=original_filename or getattr(file, "filename", None) or "",
        original_ext=ext,
        created_at=datetime.utcnow(),
        priority_set_at=datetime.utcnow(),
        tg_user_id=int(tg_user_id),
        tg_username=tg_username or None,
        payment_status="none",
    )
    db.session.add(sub)
    db.session.commit()

    return jsonify({"submission_id": sub.id})


@app.route("/api/tg/submissions/<int:submission_id>/metadata", methods=["POST"])
def tg_set_metadata(submission_id: int):
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    artist = (data.get("artist") or "").strip()
    title = (data.get("title") or "").strip()
    if not artist and not title:
        return jsonify({"error": "artist or title required"}), 400
    sub.artist = artist or sub.artist or ""
    sub.title = title or sub.title or ""
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/tg/submissions/<int:submission_id>/enqueue_free", methods=["POST"])
def tg_enqueue_free(submission_id: int):
    """Finalize file and put into queue with priority 0."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"error": "not found"}), 404
    if (sub.status or "") in ("deleted", "done"):
        return jsonify({"error": "invalid status"}), 400
    if not (sub.artist or "").strip() and not (sub.title or "").strip():
        return jsonify({"error": "missing metadata"}), 400

    _finalize_tmp_to_storage(sub)

    sub.priority = 0
    sub.status = "queued"
    sub.priority_set_at = datetime.utcnow()
    sub.payment_status = "none"
    sub.payment_provider = None
    sub.payment_ref = None
    sub.payment_amount = None
    db.session.commit()

    _broadcast_queue_state()
    return jsonify({"ok": True, "position": _queue_position(submission_id)})


@app.route("/api/tg/submissions/<int:submission_id>/waiting_payment", methods=["POST"])
def tg_waiting_payment(submission_id: int):
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    prio = int(data.get("priority") or 0)
    if prio not in (100, 200, 300, 400):
        return jsonify({"error": "bad priority"}), 400
    if not (sub.artist or "").strip() and not (sub.title or "").strip():
        return jsonify({"error": "missing metadata"}), 400

    sub.payment_status = "pending"
    sub.payment_amount = prio
    provider = (data.get("provider") or "").strip() or None
    ref = (data.get("provider_ref") or data.get("ref") or "").strip() or None
    if provider not in (None, "donationalerts"):
        return jsonify({"error": "bad provider"}), 400
    sub.payment_provider = provider
    sub.payment_ref = ref

    if (sub.status or "") not in ("queued", "playing"):
        sub.status = "waiting_payment"
    db.session.commit()
    _broadcast_queue_state()
    return jsonify({"ok": True})


@app.route("/api/tg/submissions/<int:submission_id>/mark_paid", methods=["POST"])
def tg_mark_paid(submission_id: int):
    """Mark payment as paid and finalize to queue."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True) or {}
    provider = (data.get("provider") or "").strip()
    provider_ref = (data.get("provider_ref") or "").strip()
    amount = int(data.get("amount") or 0)

    if provider not in ("donationalerts",):
        return jsonify({"error": "bad provider"}), 400
    if not provider_ref:
        return jsonify({"error": "provider_ref required"}), 400

    if sub.payment_status == "paid":
        if sub.payment_ref == provider_ref:
            return jsonify({"ok": True, "position": _queue_position(submission_id)})
        return jsonify({"error": "already paid"}), 409

    required = int(sub.payment_amount or sub.priority or 0)
    if amount < required:
        return jsonify({"error": "amount too low", "required": required}), 400

    if (sub.status or "") not in ("queued", "playing"):
        _finalize_tmp_to_storage(sub)

    sub.priority = required
    if (sub.status or "") != "playing":
        sub.status = "queued"
    sub.payment_status = "paid"
    sub.payment_provider = provider
    sub.payment_ref = provider_ref
    sub.payment_amount = required
    sub.priority_set_at = datetime.utcnow()
    db.session.commit()

    _broadcast_queue_state()
    return jsonify({"ok": True, "position": _queue_position(submission_id)})


@app.route("/api/tg/submissions/<int:submission_id>/cancel", methods=["POST"])
def tg_cancel_submission(submission_id: int):
    """Best-effort cancel submission."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"ok": True})

    status = (sub.status or "")
    if status in ("draft", "waiting_payment"):
        try:
            ext = (sub.original_ext or "").lower()
            tmp_path = _tmp_path_for(sub.file_uuid, ext)
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        sub.status = "deleted"

    sub.payment_status = "none"
    sub.payment_provider = None
    sub.payment_ref = None
    sub.payment_amount = None
    db.session.commit()
    _broadcast_queue_state()
    return jsonify({"ok": True})


@app.route("/api/tg/my_queue", methods=["GET"])
def tg_my_queue():
    """List submissions in queue belonging to tg_user_id."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    tg_user_id = request.args.get("tg_user_id") or ""
    if not tg_user_id.isdigit():
        return jsonify([])

    rows = (
        db.session.query(TrackSubmission)
        .filter(TrackSubmission.tg_user_id == int(tg_user_id))
        .filter(TrackSubmission.status.in_(["queued", "playing"]))
        .order_by(TrackSubmission.priority.desc(), TrackSubmission.priority_set_at.asc(), TrackSubmission.created_at.asc())
        .limit(50)
        .all()
    )
    items = []
    for s in rows:
        items.append({
            "id": s.id,
            "artist": s.artist or "",
            "title": s.title or "",
            "display": _submission_display_name(s),
            "priority": s.priority or 0,
            "status": s.status,
        })
    return jsonify(items)
