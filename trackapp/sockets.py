"""Socket.IO event handlers for TrackRaterAntigaz (split from monolithic app.py).

NOTE:
`from .core import *` does **not** import names that start with `_`.
This module uses several underscore-prefixed helpers from the legacy code,
so we import them explicitly.
"""

from .core import *  # noqa: F401,F403

import uuid

# Explicitly import underscore-prefixed helpers used in this module.
from .core import (
    _broadcast_playback_state,
    _broadcast_queue_state,
    _compute_playback_position_ms,
    _get_track_url,
    _get_playback_snapshot,
    _now_ms,
    _require_admin,
    _require_panel_access,
    _serialize_queue_state,
    _serialize_state,
)
from .state import _submission_display_name
from .twitch_notify import notify_twitch_bot_track_changed


# --- Rating session presence ("join rating") ---
# We deliberately keep slots visible "as if online" until explicit leave/kick.
# Reconnect just updates the socket sid.
active_raters = {}  # user_id -> {"rater_id": str, "sid": str, "username": str}


def _current_user_and_id():
    u = get_current_user()
    if not u:
        return None, None
    return u, getattr(u, "id", u.username)


def _is_joined_rater() -> bool:
    u, uid = _current_user_and_id()
    if not u or uid is None:
        return False
    return uid in active_raters


def _broadcast_raters_presence():
    # Minimal payload for UI (slot visible even if temporarily disconnected).
    try:
        payload = {
            "raters": [
                {
                    "user_id": str(uid),
                    "username": info.get("username") or "",
                    "rater_id": info.get("rater_id"),
                }
                for uid, info in active_raters.items()
            ]
        }
        socketio.emit("raters_presence_updated", payload, room="panel")
    except Exception:
        pass


@socketio.on("connect")
def handle_connect():
    # Everyone is a public viewer by default.
    try:
        from flask_socketio import join_room

        join_room("public")
        # If this user previously joined rating, restore membership.
        u, uid = _current_user_and_id()
        if uid is not None and uid in active_raters:
            active_raters[uid]["sid"] = request.sid
            join_room("raters")
            # Restore client-side flag so local playback can be blocked outside the panel.
            info = active_raters.get(uid) or {}
            emit(
                "rating_joined",
                {
                    "rater_id": info.get("rater_id"),
                    "user_id": str(uid),
                    "username": info.get("username") or getattr(u, "username", ""),
                },
            )
    except Exception:
        pass


@socketio.on("enter_panel")
def handle_enter_panel():
    if not _require_panel_access():
        return
    from flask_socketio import join_room

    join_room("panel")
    # Send a full snapshot needed for the panel UI.
    emit("initial_state", _serialize_state())
    emit("queue_state", _serialize_queue_state(limit=100))
    emit("playback_state", _get_playback_snapshot())
    # If this user already joined the rating earlier, restore UI state after refresh.
    u, uid = _current_user_and_id()
    if uid is not None and uid in active_raters:
        info = active_raters.get(uid) or {}
        emit(
            "rating_joined",
            {
                "rater_id": info.get("rater_id"),
                "user_id": str(uid),
                "username": info.get("username") or getattr(u, "username", ""),
            },
        )
    _broadcast_raters_presence()


@socketio.on("leave_panel")
def handle_leave_panel():
    try:
        from flask_socketio import leave_room

        leave_room("panel")
    except Exception:
        pass


@socketio.on("join_rating")
def handle_join_rating():
    """Присоединение к оценке по кнопке.
    Панель (слот) создаётся только в момент join.
    """
    if not _require_panel_access():
        return
    from flask_socketio import join_room

    u, uid = _current_user_and_id()
    if not u or uid is None:
        return

    # If user already joined earlier, just refresh sid/room and restore client state.
    if uid in active_raters:
        try:
            active_raters[uid]["sid"] = request.sid
        except Exception:
            pass
        try:
            join_room("raters")
        except Exception:
            pass
        info = active_raters.get(uid) or {}
        rid = info.get("rater_id")
        emit(
            "rating_joined",
            {"rater_id": rid, "user_id": str(uid), "username": info.get("username") or u.username},
        )
        emit("playback_state", _get_playback_snapshot())
        socketio.emit("initial_state", _serialize_state(), room="panel")
        _broadcast_raters_presence()
        return
    with state_lock:
        shared_state.setdefault("raters", {})
        # Generate a unique rater id; avoids global counters and survives concurrent joins.
        rid = uuid.uuid4().hex[:8]
        while rid in shared_state["raters"]:
            rid = uuid.uuid4().hex[:8]
        order = len(shared_state["raters"])
        display_name = (u.display_name or "").strip() or u.username
        shared_state["raters"][rid] = {
            "id": rid,
            "name": display_name,
            "order": order,
            "scores": {key: 0 for key, _label in CRITERIA},
            "user_id": str(uid),
        }
    active_raters[uid] = {"rater_id": rid, "sid": request.sid, "username": display_name}
    join_room("raters")

    emit("rating_joined", {"rater_id": rid, "user_id": str(uid), "username": display_name})
    emit("playback_state", _get_playback_snapshot())
    socketio.emit("initial_state", _serialize_state(), room="panel")
    _broadcast_raters_presence()


@socketio.on("leave_rating")
def handle_leave_rating():
    if not _require_panel_access():
        return
    from flask_socketio import leave_room

    _u, uid = _current_user_and_id()
    if uid is None:
        return

    info = active_raters.pop(uid, None) or {}
    rid = info.get("rater_id")
    if rid:
        with state_lock:
            try:
                raters = shared_state.get("raters") or {}
                raters.pop(str(rid), None)
                ordered = sorted(raters.values(), key=lambda r: r.get("order", 0))
                for idx, r in enumerate(ordered):
                    r["order"] = idx
            except Exception:
                pass

    try:
        leave_room("raters")
    except Exception:
        pass
    emit("rating_left", {})
    socketio.emit("initial_state", _serialize_state(), room="panel")
    _broadcast_raters_presence()


@socketio.on("kick_rater")
def handle_kick_rater(data):
    # Always reply to the caller so the UI can show feedback.
    if not _require_admin():
        try:
            emit("kick_result", {"ok": False, "msg": "not_admin"})
        except Exception:
            pass
        return
    payload = data or {}
    target_uid = payload.get("user_id")
    target_rid = payload.get("rater_id")
    if target_uid is None and target_rid is None:
        try:
            emit("kick_result", {"ok": False, "msg": "no_target"})
        except Exception:
            pass
        return

    victim_uid = None
    # Prefer kicking by explicit user_id; fallback to rater_id
    for uid, info in list(active_raters.items()):
        if target_uid is not None and str(uid) == str(target_uid):
            victim_uid = uid
            break
        if victim_uid is None and target_rid is not None and str(info.get("rater_id")) == str(target_rid):
            victim_uid = uid
            # don't break: user_id match should win if present

    if victim_uid is None:
        try:
            emit("kick_result", {"ok": False, "msg": "not_found"})
        except Exception:
            pass
        return

    info = active_raters.pop(victim_uid, None) or {}
    rid = info.get("rater_id")
    if rid:
        with state_lock:
            try:
                raters = shared_state.get("raters") or {}
                raters.pop(str(rid), None)
                ordered = sorted(raters.values(), key=lambda r: r.get("order", 0))
                for idx, r in enumerate(ordered):
                    r["order"] = idx
            except Exception:
                pass

    sid = info.get("sid")
    try:
        if sid:
            socketio.emit("kicked", {}, room=sid)
    except Exception:
        pass

    # Ack to the admin who initiated the kick.
    try:
        emit("kick_result", {"ok": True, "msg": "kicked", "user_id": str(victim_uid), "rater_id": str(rid) if rid else None})
    except Exception:
        pass

    socketio.emit("initial_state", _serialize_state(), room="panel")
    _broadcast_raters_presence()

    try:
        emit("kick_result", {"ok": True, "msg": "kicked"})
    except Exception:
        pass


@socketio.on("request_initial_state")
def handle_initial_state():
    emit("initial_state", _serialize_state())


@socketio.on("request_queue_state")
def handle_request_queue_state():
    """Состояние очереди и плеера для панели."""
    if not _require_panel_access():
        return
    emit("queue_state", _serialize_queue_state(limit=100))
    emit("playback_state", _get_playback_snapshot())


@socketio.on("admin_set_submission_priority")
def handle_admin_set_submission_priority(data):
    # Queue moderation is allowed for judges/admins (panel access).
    if not _require_panel_access():
        return
    sid = (data or {}).get("submission_id")
    pr = (data or {}).get("priority")
    try:
        sid = int(sid)
        pr = int(pr)
    except Exception:
        return

    sub = db.session.get(TrackSubmission, sid)
    if not sub or sub.status in ("deleted", "done"):
        return

    # Обновляем priority_set_at только если приоритет реально изменился.
    # Это гарантирует FIFO внутри одного уровня приоритета: более ранний 200
    # не будет перебит более поздним 200.
    if int(sub.priority or 0) != pr:
        sub.priority = pr
        sub.priority_set_at = datetime.utcnow()
    db.session.commit()
    _broadcast_queue_state()


@socketio.on("admin_delete_submission")
def handle_admin_delete_submission(data):
    # Queue moderation is allowed for judges/admins (panel access).
    if not _require_panel_access():
        return
    sid = (data or {}).get("submission_id")
    try:
        sid = int(sid)
    except Exception:
        return

    sub = db.session.get(TrackSubmission, sid)
    if not sub:
        return

    # если удаляем активный трек — остановим плеер и очистим состояние
    is_active_track = False
    try:
        with state_lock:
            if shared_state.get("active_submission_id") == sid:
                is_active_track = True
                shared_state["active_submission_id"] = None
                shared_state["playback"] = {
                    "is_playing": False,
                    "position_ms": 0,
                    "server_ts_ms": _now_ms(),
                }
                shared_state["track_name"] = ""
        # Only reset track name if we deleted the ACTIVE track
        if is_active_track:
            emit("track_name_changed", {"track_name": ""})
    except Exception:
        pass

    sub.status = "deleted"
    db.session.commit()

    # удалить файл с диска (конвертацию отключили, поэтому удаляем только исходник)
    try:
        ext = (sub.original_ext or "").lower().lstrip(".")
        raw_path = os.path.join(SUBMISSIONS_RAW_DIR, f"{sub.file_uuid}.{ext}")
        if os.path.exists(raw_path):
            os.remove(raw_path)
    except Exception:
        pass

    _broadcast_playback_state()
    _broadcast_queue_state()


@socketio.on("admin_activate_submission")
def handle_admin_activate_submission(data):
    """Сделать трек активным и (опционально) стартануть проигрывание."""
    # Queue moderation is allowed for judges/admins (panel access).
    if not _require_panel_access():
        return

    sid = (data or {}).get("submission_id")
    autoplay = bool((data or {}).get("autoplay", True))
    try:
        sid = int(sid)
    except Exception:
        return

    sub = db.session.get(TrackSubmission, sid)
    if not sub:
        return
    if sub.status in ("deleted", "failed", "converting"):
        return

    # Снимем флаг playing с предыдущего трека.
    # Важно: если трек НЕ был оценён судьями, при переключении он должен вернуться в очередь,
    # иначе он "пропадает" из UI (очередь отображает только status == "queued").
    # Перевод в "done" делаем ТОЛЬКО в обработчике судейской оценки.
    try:
        prev_playing = (
            db.session.query(TrackSubmission)
            .filter(TrackSubmission.status == "playing")
            .all()
        )
        for p in prev_playing:
            if p.id != sub.id:
                p.status = "queued"
        sub.status = "playing"
        db.session.commit()
    except Exception:
        # Не роняем активацию, даже если что-то пошло не так с БД
        pass

    track_name = _submission_display_name(sub)

    # Ensure there is a Track row linked to this submission so we can show a public track page + QR in real time.
    # This enables "live mode": viewers can open /track/<id> and leave reviews while the track is playing.
    try:
        track = None
        if sub.linked_track_id:
            try:
                track = db.session.get(Track, int(sub.linked_track_id))
            except Exception:
                track = None

        if not track:
            track = Track(name=track_name)
            track.submission_id = sub.id
            db.session.add(track)
            db.session.flush()  # assign track.id
            sub.linked_track_id = track.id
            db.session.commit()
        else:
            # Keep the display name in sync (optional but nice for widget).
            if track.name != track_name:
                track.name = track_name
                db.session.commit()

        # Broadcast live payload for public widgets (OBS).
        payload = {
            "track_id": track.id,
            "track_name": track_name,
            "track_url": _get_track_url(track.id),
            "qr_url": url_for("qr_for_track", track_id=track.id, _external=True),
        }
        socketio.emit("live_track_changed", payload, room="public")

        # Notify Twitch chat bot (best-effort). Viewers will need to log in to submit
        # a review, but the track page itself is public.
        try:
            notify_twitch_bot_track_changed(
                channel=None,
                track_id=int(track.id),
                track_name=str(track_name or ""),
                track_url_external=str(payload.get("track_url") or ""),
            )
        except Exception:
            pass
    except Exception:
        # Don't crash activation if widget broadcast fails for some reason.
        pass

    with state_lock:
        shared_state["track_name"] = track_name
        shared_state["active_submission_id"] = sub.id
        shared_state["playback"] = {
            "is_playing": bool(autoplay),
            "position_ms": 0,
            "server_ts_ms": _now_ms(),
        }

    emit("track_name_changed", {"track_name": track_name})
    _broadcast_playback_state()
    _broadcast_queue_state()

@socketio.on("admin_playback_cmd")
def handle_admin_playback_cmd(data):
    """Команды управления синхро‑плеером."""
    # Playback can be controlled by anyone who explicitly joined the rating.
    if not _is_joined_rater():
        return
    action = (data or {}).get("action")
    now = _now_ms()

    with state_lock:
        active_id = shared_state.get("active_submission_id")
        pb = dict(shared_state.get("playback") or {})

        if not active_id:
            return

        cur_pos = _compute_playback_position_ms(pb, now_ms=now)

        if action == "play":
            pb["is_playing"] = True
            pb["server_ts_ms"] = now
        elif action == "pause":
            pb["is_playing"] = False
            pb["position_ms"] = cur_pos
            pb["server_ts_ms"] = now
        elif action == "stop":
            pb["is_playing"] = False
            pb["position_ms"] = 0
            pb["server_ts_ms"] = now
        elif action == "restart":
            pb["position_ms"] = 0
            pb["server_ts_ms"] = now
        elif action == "seek":
            try:
                target_ms = int((data or {}).get("position_ms") or 0)
            except Exception:
                target_ms = 0
            target_ms = max(0, target_ms)

            # если известна длительность — ограничим
            try:
                sub = db.session.get(TrackSubmission, int(active_id))
                if sub and sub.duration_sec is not None:
                    max_ms = int(sub.duration_sec) * 1000
                    if max_ms > 0:
                        target_ms = min(target_ms, max_ms)
            except Exception:
                pass

            pb["position_ms"] = target_ms
            pb["server_ts_ms"] = now
        else:
            return

        shared_state["playback"] = pb

    _broadcast_playback_state()


@socketio.on("change_track_name")
def handle_change_track_name(data):
    if not _require_admin():
        return
    track_name = (data or {}).get("track_name", "").strip()
    with state_lock:
        shared_state["track_name"] = track_name
    emit("track_name_changed", {"track_name": track_name}, broadcast=True, include_self=True)


@socketio.on("change_rater_name")
def handle_change_rater_name(data):
    # Роль "judge" должна иметь возможность менять значения на панели,
    # иначе локально UI обновится, но остальные клиенты не получат broadcast.
    if not _require_panel_access():
        return
    rater_id = (data or {}).get("rater_id")
    name = (data or {}).get("name", "").strip()
    if not rater_id:
        return
    with state_lock:
        rater = shared_state["raters"].get(rater_id)
        if not rater:
            return
        if name:
            rater["name"] = name
        payload = {"rater_id": rater_id, "name": rater["name"]}
    emit("rater_name_changed", payload, broadcast=True, include_self=True)


@socketio.on("change_slider")
def handle_change_slider(data):
    # Слайдеры — часть панели оценки, доступна админам и роли "judge".
    if not _require_panel_access():
        return
    # But changing values is only allowed for joined raters, and only for their own slot.
    u, uid = _current_user_and_id()
    if uid is None or uid not in active_raters:
        return
    rater_id = (data or {}).get("rater_id")
    if str(rater_id) != str(active_raters[uid].get("rater_id")):
        return
    criterion_key = (data or {}).get("criterion_key")
    try:
        value = float((data or {}).get("value", 0))
    except (TypeError, ValueError):
        value = 0.0
    if not rater_id or not criterion_key:
        return
    with state_lock:
        rater = shared_state["raters"].get(rater_id)
        if not rater:
            return
        if criterion_key not in rater["scores"]:
            return
        rater["scores"][criterion_key] = value
    emit(
        "slider_updated",
        {"rater_id": rater_id, "criterion_key": criterion_key, "value": value},
        broadcast=True,
        include_self=True,
    )


@socketio.on("add_rater")
def handle_add_rater():
    """Legacy endpoint: manual add rater is disabled.
    Rater panels are created only when a user presses "join rating".
    """
    if not _require_admin():
        return
    emit("error", {"message": "Manual add_rater is disabled. Use join_rating."})

@socketio.on("remove_rater")
def handle_remove_rater(data):
    if not _require_admin():
        return
    rater_id = (data or {}).get("rater_id")
    if not rater_id:
        return
    with state_lock:
        if rater_id not in shared_state["raters"]:
            return
        shared_state["raters"].pop(rater_id)
        for idx, rid in enumerate(
            sorted(shared_state["raters"].keys(), key=lambda x: int(x))
        ):
            shared_state["raters"][rid]["order"] = idx
    emit("rater_removed", {"rater_id": rater_id})


@socketio.on("evaluate")
def handle_evaluate():
    if not _require_admin():
        return

    rater_results = []

    with state_lock:
        track_name = shared_state["track_name"] or "Без названия"
        active_submission_id = shared_state.get("active_submission_id")
        raters_list = list(shared_state["raters"].values())
        raters_list.sort(key=lambda r: r.get("order", 0))

    if not raters_list:
        return

        # Reuse an existing Track linked to the active submission (created when it was activated),
    # so the public /track/<id> page (and QR) stays stable during the live stream.
    track = None
    sub_for_track = None
    if active_submission_id:
        try:
            sub_for_track = db.session.get(TrackSubmission, int(active_submission_id))
        except Exception:
            sub_for_track = None
    
    if sub_for_track and sub_for_track.linked_track_id:
        track = db.session.get(Track, int(sub_for_track.linked_track_id))
    
    if not track:
        track = Track(name=track_name)
        if active_submission_id:
            try:
                track.submission_id = int(active_submission_id)
            except Exception:
                track.submission_id = None
        db.session.add(track)
        db.session.flush()
    
        # If this track came from the queue, link it back to the submission so the live widget can point to it.
        if sub_for_track and not sub_for_track.linked_track_id:
            sub_for_track.linked_track_id = track.id
            db.session.commit()
    else:
        # Keep name in sync with the current live name.
        if track.name != track_name:
            track.name = track_name
            db.session.commit()
    
        # Если трек пришёл из очереди — пометим submission как "done" и свяжем с Track.
        if getattr(track, "submission_id", None):
            try:
                sub = db.session.get(TrackSubmission, int(track.submission_id))
                if sub and sub.status not in ("deleted", "failed", "converting"):
                    sub.linked_track_id = track.id
                    # если уже играли, то по факту он теперь оценён
                    if sub.status in ("queued", "playing"):
                        sub.status = "done"
            except Exception as e:
                print("Warning: could not link submission to track:", e)
    
        rater_results = []
        for r in raters_list:
            scores = r["scores"]
            vals = list(scores.values())
            avg = sum(vals) / len(vals) if vals else 0.0
            rater_results.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "scores": scores,
                    "average": round(avg, 2),
                }
            )
            for ck, val in scores.items():
                db.session.add(
                    Evaluation(
                        track_id=track.id,
                        rater_name=r["name"],
                        criterion_key=ck,
                        score=float(val),
                    )
                )
    
    criterion_avgs = []
    num_raters = len(raters_list)
    for key, label in CRITERIA:
        total = 0.0
        for r in raters_list:
            total += float(r["scores"].get(key, 0.0))
        avg = total / num_raters if num_raters else 0.0
        criterion_avgs.append(
            {"key": key, "label": label, "average": round(avg, 2)}
        )

    overall = (
        sum(c["average"] for c in criterion_avgs) / len(criterion_avgs)
        if criterion_avgs
        else 0.0
    )

    db.session.commit()

    # После оценки трека из очереди — убираем его из текущего воспроизведения,
    # чтобы он исчезал из очереди (status=done) и не оставался "активным сейчас".
    if active_submission_id:
        try:
            with state_lock:
                # фиксируем остановку плеера для всех
                shared_state["active_submission_id"] = None
                shared_state["playback"] = {
                    "is_playing": False,
                    "position_ms": 0,
                    "server_ts_ms": _now_ms(),
                }
        except Exception:
            pass

        # Сообщаем всем клиентам сразу: очередь обновилась и активного трека больше нет.
        try:
            _broadcast_playback_state()
            _broadcast_queue_state()
        except Exception:
            pass

    # рассчитываем средний балл по треку так же, как для страницы топа
    track_avg = (
        db.session.query(func.avg(Evaluation.score))
        .filter(Evaluation.track_id == track.id)
        .scalar()
        or 0.0
    )

    # подзапрос со средними оценками по трекам
    # подзапрос со средними оценками по трекам
    # УЧИТЫВАЕМ только треки, не удалённые из топа
    avg_subq = (
        db.session.query(
            Evaluation.track_id.label("tid"),
            func.avg(Evaluation.score).label("avg_score"),
        )
        .join(Track, Track.id == Evaluation.track_id)
        .filter(Track.is_deleted.is_(False))
        .group_by(Evaluation.track_id)
        .subquery()
    )

    # сколько треков (НЕ удалённых) имеют средний балл строго выше текущего
    better_count = (
            db.session.query(func.count())
            .filter(avg_subq.c.avg_score > track_avg)
            .scalar()
            or 0
    )

    top_position = int(better_count) + 1


    qr_url = url_for("qr_for_track", track_id=track.id, _external=True)
    track_url = _get_track_url(track.id)
    payload = {
        "track_id": track.id,
        "track_name": track_name,
        "track_url": track_url,
        "qr_url": qr_url,
        "raters": rater_results,
        "criteria": criterion_avgs,
        "overall": round(overall, 2),
        "top_position": top_position,
    }
    emit("evaluation_result", payload, broadcast=True)


@socketio.on("reset_state")
def handle_reset_state():
    if not _require_admin():
        return
    old_active_id = None
    with state_lock:
        shared_state["track_name"] = ""
        old_active_id = shared_state.get("active_submission_id")
        shared_state["active_submission_id"] = None
        shared_state["playback"] = {
            "is_playing": False,
            "position_ms": 0,
            "server_ts_ms": _now_ms(),
        }
        for r in shared_state["raters"].values():
            r["scores"] = {key: 0 for key, _ in CRITERIA}

    # если сбросили состояние во время проигрывания — вернём трек обратно в очередь (если он не оценён)
    if old_active_id:
        try:
            sub = db.session.get(TrackSubmission, int(old_active_id))
            if sub and sub.status == "playing" and not sub.linked_track_id:
                sub.status = "queued"
                db.session.commit()
        except Exception:
            pass

    emit("state_reset", _serialize_state())
    _broadcast_playback_state()
    _broadcast_queue_state()

