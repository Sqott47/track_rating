"""Shared in-memory state and Socket.IO helper functions.

This module contains the legacy server-authoritative state used for live rating
synchronization and queue/playback broadcasts.
"""

import os
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

from flask import request, session, url_for
from flask_socketio import emit
from sqlalchemy import func

from .extensions import (
    app,
    db,
    socketio,
    CRITERIA,
    DEFAULT_NUM_RATERS,
)
from .models import (
    TrackSubmission,
    Track,
    Evaluation,
    ViewerRating,
    TrackComment,
    StreamConfig,
    User,
)

state_lock = threading.Lock(
)
_next_rater_id = 1
shared_state = {
    "track_name": "",
    "raters": {},  # rater_id -> {id, name, order, scores{criterion_key: value}}
    # Активный трек из очереди (track_submissions.id). None если трек задан вручную.
    "active_submission_id": None,
    # Состояние синхро-плеера.
    "playback": {
        "is_playing": False,
        "position_ms": 0,
        "server_ts_ms": 0,
    },
}





def _is_safe_uuid(value: str) -> bool:
    """Validate that a value looks like uuid4().hex (32 lowercase hex).

    We keep it permissive (up to 64 chars) to support possible future formats,
    but still restrict to lowercase hex to prevent path traversal.
    """
    if not value:
        return False
    if len(value) > 64:
        return False
    for ch in value:
        if ch not in "0123456789abcdef":
            return False
    return True


def _get_track_url(track_id: int) -> str:
    """Canonical external link to the public track page."""
    return url_for("track_page", track_id=track_id, _external=True)


def _is_image_filename(filename: str) -> bool:
    if not filename:
        return False
    lname = filename.lower()
    return any(lname.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"))


def get_current_user():
    username = session.get("user")
    if not username:
        return None
    return db.session.query(User).filter_by(username=username).first()


# Make `current_user` available in all templates.
# Many templates (base.html) derive JS flags like __IS_ADMIN__/__IS_JUDGE__
# from this variable. If it is missing, the UI can incorrectly hide
# privileged controls (e.g. queue moderation buttons), especially under
# Turbo Drive where app.js is not reloaded.
@app.context_processor
def _inject_current_user():
    return {"current_user": get_current_user()}


@app.before_request
def _enforce_session_version():
    """Invalidate sessions after password change / logout-everywhere.

    We keep auth very lightweight (no Flask-Login). Session stores username/role
    plus a session_version snapshot. If DB session_version changes, the session
    becomes invalid.
    """
    username = session.get("user")
    if not username:
        return

    # Avoid extra DB queries for static files.
    if request.endpoint == "static":
        return

    u = db.session.query(User).filter_by(username=username).first()
    if not u:
        session.pop("user", None)
        session.pop("role", None)
        session.pop("session_version", None)
        return

    sess_v = int(session.get("session_version") or 1)
    db_v = int(u.session_version or 1)
    if sess_v != db_v:
        session.pop("user", None)
        session.pop("role", None)
        session.pop("session_version", None)
        # Don't force redirect for API calls; for pages it'll naturally show login.
        return


def _require_admin() -> bool:
    user = get_current_user()
    if not user:
        return False
    return user.is_admin()


def _require_superadmin() -> bool:
    user = get_current_user()
    if not user:
        return False
    return user.is_superadmin()



def _require_panel_access() -> bool:
    """
    Доступ к панели оценки: админы, супер‑админ и роль "judge".
    """
    user = get_current_user()
    if not user:
        return False
    return user.is_judge()
def _init_default_raters():
    """Backwards-compatible no-op.
    Раньше создавались DEFAULT_NUM_RATERS пустых слотов. Теперь слоты
    создаются только когда пользователь нажимает «Присоединиться к оценке».
    """
    return


def _serialize_state():
    with state_lock:
        raters = list(shared_state["raters"].values())
        raters.sort(key=lambda r: r.get("order", 0))
        return {
            "track_name": shared_state["track_name"],
            "raters": raters,
            "criteria": [{"key": k, "label": label} for k, label in CRITERIA],
        }


def _now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def _submission_display_name(sub: TrackSubmission) -> str:
    artist = (sub.artist or "").strip()
    title = (sub.title or "").strip()
    if artist and title:
        return f"{artist} — {title}"
    return title or artist or "Без названия"

def _get_submission_audio_url(file_uuid: str, ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    return url_for("submission_audio", file_uuid=file_uuid, ext=ext, _external=False)


def _compute_playback_position_ms(pb: Dict[str, Any], now_ms: Optional[int] = None) -> int:
    """Текущая позиция плеера по состоянию сервера."""
    if now_ms is None:
        now_ms = _now_ms()
    base = int(pb.get("position_ms") or 0)
    if pb.get("is_playing"):
        started_at = int(pb.get("server_ts_ms") or now_ms)
        base += max(0, now_ms - started_at)
    return max(0, base)


def _get_playback_snapshot() -> Dict[str, Any]:
    """Снимок состояния синхро‑плеера + активный трек."""
    with state_lock:
        active_id = shared_state.get("active_submission_id")
        pb = dict(shared_state.get("playback") or {})

    now_ms = _now_ms()
    pos_ms = _compute_playback_position_ms(pb, now_ms=now_ms)

    active_payload = None
    if active_id:
        sub = db.session.get(TrackSubmission, int(active_id))
        if sub and sub.status not in ("deleted", "failed"):
            active_payload = {
                "id": sub.id,
                "artist": sub.artist,
                "title": sub.title,
                "display_name": _submission_display_name(sub),
                "priority": int(sub.priority or 0),
                "status": sub.status,
                "duration_sec": int(sub.duration_sec) if sub.duration_sec is not None else None,
                "file_uuid": sub.file_uuid,
                "audio_url": _get_submission_audio_url(sub.file_uuid, sub.original_ext),
            }

    return {
        "active": active_payload,
        "playback": {
            "is_playing": bool(pb.get("is_playing")),
            "position_ms": pos_ms,
        },
    }


def _serialize_queue_state(limit: int = 50) -> Dict[str, Any]:
    """Сериализация очереди для панели/публичной страницы."""
    items = (
        db.session.query(TrackSubmission)
        .filter(TrackSubmission.status == "queued")
        .order_by(
            TrackSubmission.priority.desc(),
            TrackSubmission.priority_set_at.asc(),
            TrackSubmission.created_at.asc(),
            TrackSubmission.id.asc(),
        )
        .limit(limit)
        .all()
    )

    queued_pos = 0
    out_items: List[Dict[str, Any]] = []
    for s in items:
        pos = None
        if s.status == "queued":
            queued_pos += 1
            pos = queued_pos
        out_items.append(
            {
                "id": s.id,
                "artist": s.artist,
                "title": s.title,
                "display_name": _submission_display_name(s),
                "priority": int(s.priority or 0),
                "status": s.status,
                "duration_sec": int(s.duration_sec) if s.duration_sec is not None else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "queue_position": pos,
            }
        )

    counts = {

        "queued": db.session.query(func.count(TrackSubmission.id)).filter(TrackSubmission.status == "queued").scalar() or 0,

    }
    return {"items": out_items, "counts": counts}


def _broadcast_queue_state() -> None:
    try:
        payload = _serialize_queue_state(limit=100)
        # Queue is visible both in the panel and to public viewers.
        socketio.emit("queue_state", payload, room="panel")
        socketio.emit("queue_state", payload, room="public")
    except Exception as e:
        print("Warning: failed to broadcast queue_state:", e)


def _broadcast_playback_state() -> None:
    try:
        payload = _get_playback_snapshot()
        # Playback sync is for joined raters everywhere + observers currently in panel.
        socketio.emit("playback_state", payload, room="raters")
        socketio.emit("playback_state", payload, room="panel")
    except Exception as e:
        print("Warning: failed to broadcast playback_state:", e)


def _convert_submission_worker(submission_id: int) -> None:
    """Конвертация временно отключена.

    Исторически очередь поддерживала перекодирование wav -> mp3, но это
    создавало блокировки/залипания в реальном времени. Сейчас храним
    исходники как есть (mp3/wav) и ничего не конвертируем.
    """
    return


