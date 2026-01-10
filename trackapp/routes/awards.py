"""Awards routes: awards CRUD, nominations, winner management.

Migrated from the monolithic routes.py for better maintainability.
"""

import json
import os
import uuid
from datetime import datetime

from flask import request, redirect, url_for, flash, render_template, jsonify

from ..core import app, db, get_current_user
from ..extensions import ALLOWED_SUBMISSION_EXTS, AWARDS_UPLOAD_DIR, secure_filename
from ..models import Award, AwardNomination, Track, TrackSubmission

# Import notification helper from tg_bot module
try:
    from .tg_bot import _notify_submission_tg
except ImportError:
    def _notify_submission_tg(sub, text):
        pass


# -----------------
# Helpers
# -----------------

def _track_audio_url_for_embed(track: Track):
    """Best-effort audio URL for embedded players."""
    try:
        if getattr(track, "submission_id", None):
            sub = db.session.get(TrackSubmission, int(track.submission_id))
            if sub and sub.status not in ("deleted", "failed"):
                ext = (sub.original_ext or "").lower().lstrip(".")
                if ext in ALLOWED_SUBMISSION_EXTS:
                    return url_for("submission_audio", file_uuid=sub.file_uuid, ext=ext)
    except Exception:
        return None
    return None


def _award_winner_display(award: Award):
    """Return a dict with winner display data (snapshot-first)."""
    if not award:
        return None
    if award.winner_snapshot_json:
        try:
            return json.loads(award.winner_snapshot_json)
        except Exception:
            pass
    if award.winner_nomination_id:
        nom = db.session.get(AwardNomination, int(award.winner_nomination_id))
        if nom and nom.track:
            return {
                "track_id": nom.track.id,
                "track_name": nom.track.name,
                "winner_at": None,
            }
    return None


def _award_store_image_file(f) -> str | None:
    """Store uploaded award image under static/uploads/awards and return URL path."""
    filename = secure_filename(f.filename or "")
    if not filename:
        return None
    ext = (filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "webp", "gif"}:
        return None
    uid = str(uuid.uuid4())
    stored = f"{uid}.{ext}"
    abs_path = os.path.join(AWARDS_UPLOAD_DIR, stored)
    os.makedirs(AWARDS_UPLOAD_DIR, exist_ok=True)
    f.save(abs_path)
    return url_for("static", filename=f"uploads/awards/{stored}")


# -----------------
# Awards Page
# -----------------

@app.route("/awards")
def awards_page():
    user = get_current_user()
    active_awards = (
        db.session.query(Award)
        .filter(Award.status != "ended")
        .order_by(Award.created_at.desc())
        .all()
    )
    ended_awards = (
        db.session.query(Award)
        .filter(Award.status == "ended")
        .order_by(Award.created_at.desc())
        .all()
    )

    selected = None
    q_award_id = request.args.get("award_id")
    if q_award_id:
        try:
            selected = db.session.get(Award, int(q_award_id))
        except Exception:
            selected = None
    if not selected:
        try:
            selected = (
                db.session.query(Award)
                .filter(Award.status == "active")
                .order_by(Award.created_at.desc())
                .first()
            )
            if not selected:
                selected = (
                    db.session.query(Award)
                    .filter(Award.status != "draft")
                    .order_by(Award.created_at.desc())
                    .first()
                )
        except Exception:
            selected = None

    return render_template(
        "awards.html",
        active_awards=active_awards,
        ended_awards=ended_awards,
        selected_award=selected,
        is_admin=bool(user and user.is_admin()),
    )


# -----------------
# Awards CRUD
# -----------------

@app.route("/awards", methods=["POST"])
def awards_create():
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(url_for("awards_page"))

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    icon_emoji = (request.form.get("icon_emoji") or "").strip() or None
    if not title:
        flash("Название премии обязательно", "error")
        return redirect(url_for("awards_page"))

    a = Award(
        title=title,
        description=description,
        icon_emoji=icon_emoji,
        status="active",
        created_by_user_id=user.id,
    )
    db.session.add(a)
    db.session.commit()

    try:
        f = request.files.get("image")
        if f and getattr(f, "filename", ""):
            stored = _award_store_image_file(f)
            if stored:
                a.image_path = stored
                db.session.add(a)
                db.session.commit()
    except Exception:
        pass
    flash("Премия создана", "success")
    return redirect(url_for("awards_page", award_id=a.id))


@app.route("/awards/<int:award_id>/update", methods=["POST"])
def awards_update(award_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(url_for("awards_page"))

    award = db.session.get(Award, award_id)
    if not award:
        flash("Премия не найдена", "error")
        return redirect(url_for("awards_page"))

    if (award.status or "") == "ended":
        flash("Премия завершена — редактирование запрещено", "error")
        return redirect(url_for("awards_page", award_id=award.id))

    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Название премии обязательно", "error")
        return redirect(url_for("awards_page", award_id=award.id))

    award.title = title
    award.description = (request.form.get("description") or "").strip() or None
    award.icon_emoji = (request.form.get("icon_emoji") or "").strip() or None

    if (request.form.get("clear_image") or "") == "1":
        award.image_path = None

    try:
        f = request.files.get("image")
        if f and getattr(f, "filename", ""):
            stored = _award_store_image_file(f)
            if stored:
                award.image_path = stored
    except Exception:
        pass

    db.session.add(award)
    db.session.commit()
    flash("Премия обновлена", "success")
    return redirect(url_for("awards_page", award_id=award.id))


@app.route("/awards/<int:award_id>/delete", methods=["POST"])
def awards_delete(award_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(url_for("awards_page"))

    award = db.session.get(Award, award_id)
    if not award:
        return redirect(url_for("awards_page"))

    if (award.status or "") == "ended":
        flash("Премия завершена — удаление запрещено", "error")
        return redirect(url_for("awards_page", award_id=award.id))

    award.winner_nomination_id = None
    award.winner_snapshot_json = None
    db.session.add(award)
    db.session.commit()

    db.session.delete(award)
    db.session.commit()
    flash("Премия удалена", "success")
    return redirect(url_for("awards_page"))


# -----------------
# Awards Panel (partial)
# -----------------

@app.route("/awards/<int:award_id>/panel")
def awards_panel(award_id: int):
    award = db.session.get(Award, award_id)
    if not award:
        return "<div class='award-panel-empty'>Премия не найдена</div>", 404

    nominations = (
        db.session.query(AwardNomination)
        .filter(AwardNomination.award_id == award_id)
        .order_by(AwardNomination.nominated_at.desc())
        .all()
    )

    nominees = []
    for nom in nominations:
        t = nom.track
        if not t or getattr(t, "is_deleted", False):
            continue
        audio_url = _track_audio_url_for_embed(t)
        player_title = None
        player_subtitle = None
        try:
            if getattr(t, "submission_id", None):
                sub = db.session.get(TrackSubmission, int(t.submission_id))
                if sub:
                    player_title = sub.title
                    player_subtitle = sub.artist
        except Exception:
            pass
        if not player_title:
            player_title = getattr(t, "name", "—")
        nominees.append({
            "nom": nom,
            "track": t,
            "audio_url": audio_url,
            "player_title": player_title,
            "player_subtitle": player_subtitle,
        })

    winner = _award_winner_display(award)
    user = get_current_user()

    return render_template(
        "partials/award_panel.html",
        award=award,
        nominees=nominees,
        winner=winner,
        is_admin=bool(user and user.is_admin()),
    )


# -----------------
# Nominations
# -----------------

@app.route("/awards/<int:award_id>/nominate/<int:track_id>", methods=["POST"])
def award_nominate(award_id: int, track_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(request.referrer or url_for("awards_page"))

    award = db.session.get(Award, award_id)
    track = db.session.get(Track, track_id)
    if not award or not track or getattr(track, "is_deleted", False):
        flash("Не удалось номинировать трек", "error")
        return redirect(request.referrer or url_for("top_tracks"))

    if (award.status or "") != "active":
        flash("Премия завершена — номинирование закрыто", "error")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))

    existing = (
        db.session.query(AwardNomination)
        .filter_by(award_id=award_id, track_id=track_id)
        .first()
    )
    if existing:
        flash("Трек уже номинирован", "info")
        return redirect(request.referrer or url_for("awards_page"))

    nom = AwardNomination(award_id=award_id, track_id=track_id, nominated_by_user_id=user.id)
    db.session.add(nom)
    db.session.commit()
    flash("Трек номинирован", "success")

    try:
        sub = None
        if getattr(track, "submission_id", None):
            sub = db.session.get(TrackSubmission, int(track.submission_id))
        track_title = f"{sub.artist} — {sub.title}" if sub else (getattr(track, "name", "—") or "—")
        _notify_submission_tg(sub, f"🏆 Твой трек «{track_title}» номинирован в премии «{award.title}»\n🎵")
    except Exception:
        pass

    if request.headers.get("Turbo-Frame") == "award-panel":
        return redirect(url_for("awards_panel", award_id=award_id))
    return redirect(request.referrer or url_for("awards_page"))


@app.route("/awards/nomination/<int:nom_id>/remove", methods=["POST"])
def award_remove_nomination(nom_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(request.referrer or url_for("awards_page"))
    nom = db.session.get(AwardNomination, nom_id)
    if not nom:
        return redirect(request.referrer or url_for("awards_page"))

    award = db.session.get(Award, nom.award_id)
    if award and (award.status or "") != "active":
        flash("Премия завершена — менять номинантов нельзя", "error")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))
    if award and award.winner_nomination_id == nom.id:
        award.winner_nomination_id = None
        award.winner_snapshot_json = None
        db.session.add(award)

    db.session.delete(nom)
    db.session.commit()

    if request.headers.get("Turbo-Frame") == "award-panel":
        return redirect(url_for("awards_panel", award_id=award.id))
    return redirect(request.referrer or url_for("awards_page"))


# -----------------
# Winner Management
# -----------------

@app.route("/awards/<int:award_id>/set_winner/<int:nom_id>", methods=["POST"])
def award_set_winner(award_id: int, nom_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(request.referrer or url_for("awards_page"))

    award = db.session.get(Award, award_id)
    nom = db.session.get(AwardNomination, nom_id)
    if not award or not nom or nom.award_id != award_id:
        flash("Не удалось назначить победителя", "error")
        return redirect(request.referrer or url_for("awards_page"))

    if (award.status or "") != "active":
        flash("Премия завершена — смена победителя запрещена", "error")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))

    t = nom.track
    snap = {
        "track_id": t.id if t else None,
        "track_name": t.name if t else "—",
        "winner_at": datetime.utcnow().isoformat(),
        "award_id": award.id,
        "award_title": award.title,
    }

    award.winner_nomination_id = nom.id
    award.winner_snapshot_json = json.dumps(snap, ensure_ascii=False)
    db.session.add(award)
    db.session.commit()

    try:
        sub = None
        if t and getattr(t, "submission_id", None):
            sub = db.session.get(TrackSubmission, int(t.submission_id))
        track_title = f"{sub.artist} — {sub.title}" if sub else (t.name if t else "—")
        _notify_submission_tg(sub, f"🎉 Твой трек «{track_title}» победил в премии «{award.title}»\n 🏅")
    except Exception:
        pass

    if request.headers.get("Turbo-Frame") == "award-panel":
        return redirect(url_for("awards_panel", award_id=award_id))
    return redirect(request.referrer or url_for("awards_page"))


@app.route("/awards/<int:award_id>/unset_winner", methods=["POST"])
def award_unset_winner(award_id: int):
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(request.referrer or url_for("awards_page"))

    award = db.session.get(Award, award_id)
    if not award:
        return redirect(request.referrer or url_for("awards_page"))

    if (award.status or "") != "active":
        flash("Премия завершена — победителя снимать нельзя", "error")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))

    award.winner_nomination_id = None
    award.winner_snapshot_json = None
    db.session.add(award)
    db.session.commit()

    if request.headers.get("Turbo-Frame") == "award-panel":
        return redirect(url_for("awards_panel", award_id=award_id))
    return redirect(request.referrer or url_for("awards_page"))


@app.route("/awards/<int:award_id>/end", methods=["POST"])
def award_end(award_id: int):
    """Freeze award: no more nominations and winner changes."""
    user = get_current_user()
    if not (user and user.is_admin()):
        flash("Доступ запрещён", "error")
        return redirect(request.referrer or url_for("awards_page"))

    award = db.session.get(Award, award_id)
    if not award:
        flash("Премия не найдена", "error")
        return redirect(request.referrer or url_for("awards_page"))

    if (award.status or "") == "ended":
        flash("Премия уже завершена", "info")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))

    if not award.winner_nomination_id and not award.winner_snapshot_json:
        flash("Сначала выберите победителя", "error")
        return redirect(request.referrer or url_for("awards_page", award_id=award.id))

    award.status = "ended"
    db.session.add(award)
    db.session.commit()

    flash("Премия завершена", "success")
    if request.headers.get("Turbo-Frame") == "award-panel":
        return redirect(url_for("awards_panel", award_id=award.id))
    return redirect(request.referrer or url_for("awards_page", award_id=award.id))
