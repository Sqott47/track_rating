"""Public routes: home, queue, track pages, top tracks, viewers.

Migrated from the monolithic routes.py for better maintainability.
"""

import os

from flask import request, redirect, url_for, flash, render_template, make_response, jsonify
from sqlalchemy import func

from ..core import (
    app, db, get_current_user,
    _get_or_create_viewer_id,
    _get_playback_snapshot,
    _get_s3_client,
    _is_image_filename,
    _is_safe_uuid,
    _require_admin,
    _s3_is_configured,
    _s3_key_for_submission,
    _serialize_queue_state,
)
from ..extensions import (
    ALLOWED_SUBMISSION_EXTS,
    CRITERIA,
    S3_BUCKET,
    S3_PRESIGN_EXPIRES,
    SUBMISSION_MAX_MB,
    SUBMISSIONS_RAW_DIR,
    UPLOAD_DIR,
    VIEWER_COOKIE_MAX_AGE,
    VIEWER_COOKIE_NAME,
    send_from_directory,
)
from ..models import (
    Award,
    AwardNomination,
    Evaluation,
    News,
    NewsAttachment,
    StreamConfig,
    Track,
    TrackComment,
    TrackReview,
    TrackReviewScore,
    TrackSubmission,
    ViewerRating,
)


# -----------------
# Error Handlers
# -----------------

@app.errorhandler(413)
def request_entity_too_large(e):
    """Единый обработчик для слишком больших загрузок."""
    flash(f"Файл слишком большой. Максимум {SUBMISSION_MAX_MB} МБ.", "error")
    try:
        if request.path.startswith("/queue"):
            return redirect(url_for("queue_page"))
    except Exception:
        pass
    return redirect(url_for("home"))


# -----------------
# Media Files
# -----------------

@app.route("/media/submissions/<string:file_uuid>.<string:ext>")
def submission_audio(file_uuid: str, ext: str):
    """Публичная раздача загруженных аудиофайлов (mp3/wav) с поддержкой Range."""
    if not _is_safe_uuid(file_uuid):
        return make_response("Not found", 404)

    ext = (ext or "").lower().lstrip(".")
    if ext not in ALLOWED_SUBMISSION_EXTS:
        return make_response("Not found", 404)

    s3 = _get_s3_client()
    if s3 and _s3_is_configured():
        try:
            key = _s3_key_for_submission(file_uuid, ext)
            url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": S3_BUCKET, "Key": key},
                ExpiresIn=S3_PRESIGN_EXPIRES,
            )
            return redirect(url, code=302)
        except Exception as e:
            print("S3 presign error (fallback to local):", e)

    filename = f"{file_uuid}.{ext}"
    file_path = os.path.join(SUBMISSIONS_RAW_DIR, filename)
    if not os.path.isfile(file_path):
        return make_response("Not found", 404)

    resp = send_from_directory(SUBMISSIONS_RAW_DIR, filename, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# -----------------
# Queue
# -----------------

@app.route("/queue", methods=["GET"])
def queue_page():
    """Публичная очередь треков + форма загрузки."""
    active = _get_playback_snapshot().get("active")
    queue = _serialize_queue_state(limit=200)
    return render_template(
        "queue.html",
        queue_items=queue.get("items") or [],
        queue_counts=queue.get("counts") or {},
        active_track=active,
        max_mb=SUBMISSION_MAX_MB,
        allowed_exts=sorted(ALLOWED_SUBMISSION_EXTS),
    )


@app.route("/queue/submit", methods=["POST"])
def queue_submit():
    """Upload via website is disabled (live queue only)."""
    return jsonify({"error": "upload_disabled"}), 410


# -----------------
# Home Page
# -----------------

@app.route("/")
def home():
    """Публичная главная страница ANTIGAZ Hub."""
    page = request.args.get("page", 1, type=int)
    per_page = 10

    news_query = db.session.query(News).order_by(News.created_at.desc())
    news_pagination = news_query.paginate(page=page, per_page=per_page, error_out=False)

    page_news_ids = [n.id for n in news_pagination.items]
    attachments_by_news = {nid: [] for nid in page_news_ids}

    if page_news_ids:
        try:
            rows = (
                db.session.query(NewsAttachment)
                .filter(NewsAttachment.news_id.in_(page_news_ids))
                .order_by(NewsAttachment.uploaded_at.asc())
                .all()
            )
            for att in rows:
                attachments_by_news.setdefault(att.news_id, []).append({
                    "stored": att.stored_filename,
                    "original": att.original_filename,
                    "is_image": _is_image_filename(att.stored_filename),
                    "url": url_for("static", filename="uploads/news/" + att.stored_filename),
                })
        except Exception:
            pass

    # legacy attachments
    try:
        _home_filenames = os.listdir(UPLOAD_DIR)
    except FileNotFoundError:
        _home_filenames = []

    for nid in page_news_ids:
        if attachments_by_news.get(nid):
            continue
        _prefix = f"news_{nid}_"
        for _fname in _home_filenames:
            if _fname.startswith(_prefix):
                attachments_by_news[nid] = [{
                    "stored": _fname,
                    "original": _fname.replace(_prefix, "", 1) or _fname,
                    "is_image": _is_image_filename(_fname),
                    "url": url_for("static", filename="uploads/" + _fname),
                }]
                break

    news_items = []
    for n in news_pagination.items:
        attachments = attachments_by_news.get(n.id, []) or []
        news_items.append({
            "id": n.id,
            "title": n.title,
            "text": n.text,
            "tag": n.tag,
            "date": n.created_at.strftime("%d.%m.%Y") if n.created_at else None,
            "attachments": attachments,
        })

    # Mini top (top 3 by streamer avg)
    viewer_subq = (
        db.session.query(
            ViewerRating.track_id.label("v_track_id"),
            func.avg(ViewerRating.score).label("avg_viewers"),
        )
        .group_by(ViewerRating.track_id)
        .subquery()
    )

    base_query = (
        db.session.query(
            Track.id.label("track_id"),
            Track.name.label("track_name"),
            Track.created_at.label("created_at"),
            func.avg(Evaluation.score).label("avg_streamers"),
            viewer_subq.c.avg_viewers,
        )
        .join(Evaluation, Evaluation.track_id == Track.id)
        .outerjoin(viewer_subq, viewer_subq.c.v_track_id == Track.id)
        .filter(Track.is_deleted.is_(False))
        .group_by(Track.id, Track.name, Track.created_at, viewer_subq.c.avg_viewers)
    )

    top_rows = (
        base_query
        .order_by(func.avg(Evaluation.score).desc(), Track.created_at.desc())
        .limit(3)
        .all()
    )
    top_tracks = [
        {"id": row.track_id, "name": row.track_name, "avg_score": float(row.avg_streamers) if row.avg_streamers else None}
        for row in top_rows
    ]

    # Recent rated tracks
    recent_rows = (
        db.session.query(
            Track.id.label("track_id"),
            Track.name.label("track_name"),
            Track.created_at.label("created_at"),
            func.avg(Evaluation.score).label("avg_streamers"),
        )
        .join(Evaluation, Evaluation.track_id == Track.id)
        .filter(Track.is_deleted.is_(False))
        .group_by(Track.id, Track.name, Track.created_at)
        .order_by(Track.created_at.desc())
        .limit(3)
        .all()
    )
    recent_tracks = [
        {"id": row.track_id, "name": row.track_name, "created_at": row.created_at, "final_score": float(row.avg_streamers) if row.avg_streamers else None}
        for row in recent_rows
    ]

    cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
    stream_info = None
    if cfg and cfg.is_active and cfg.url:
        stream_info = {"title": cfg.title or "Стрим на Twitch", "url": cfg.url}

    return render_template(
        "home.html",
        news_items=news_items,
        news_pagination=news_pagination,
        top_tracks=top_tracks,
        recent_tracks=recent_tracks,
        stream_info=stream_info,
    )


# -----------------
# Track Page
# -----------------

@app.route("/track/<int:track_id>", methods=["GET"])
def track_page(track_id: int):
    """Public track page (server-rendered)."""
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        flash("Трек не найден", "error")
        return redirect(url_for("top_tracks"))

    user = get_current_user()
    is_admin = bool(user and user.is_admin())

    my_review = None
    if user:
        my_review = (
            db.session.query(TrackReview)
            .filter(TrackReview.track_id == track.id, TrackReview.user_id == user.id)
            .first()
        )

    my_review_score_map = {key: 0 for key, _label in CRITERIA}
    if my_review and getattr(my_review, "scores", None):
        try:
            for s in my_review.scores:
                if s.criterion_key in my_review_score_map:
                    my_review_score_map[s.criterion_key] = int(s.score)
        except Exception:
            pass

    overall_avg = db.session.query(func.avg(Evaluation.score)).filter(Evaluation.track_id == track.id).scalar()
    overall_avg = float(overall_avg) if overall_avg is not None else None

    crit_rows = (
        db.session.query(Evaluation.criterion_key, func.avg(Evaluation.score).label("avg_score"))
        .filter(Evaluation.track_id == track.id)
        .group_by(Evaluation.criterion_key)
        .order_by(Evaluation.criterion_key)
        .all()
    )
    criteria_stats = [{"key": row.criterion_key, "avg": float(row.avg_score)} for row in crit_rows]

    rater_rows = (
        db.session.query(Evaluation.rater_name, func.avg(Evaluation.score).label("avg_score"))
        .filter(Evaluation.track_id == track.id)
        .group_by(Evaluation.rater_name)
        .order_by(Evaluation.rater_name)
        .all()
    )
    raters_stats = [{"name": row.rater_name, "avg": float(row.avg_score)} for row in rater_rows]

    review_overall_val = db.session.query(func.avg(TrackReview.overall)).filter(TrackReview.track_id == track.id).scalar()
    review_overall = float(review_overall_val) if review_overall_val is not None else None
    review_count = db.session.query(func.count(TrackReview.id)).filter(TrackReview.track_id == track.id).scalar() or 0
    reviews = db.session.query(TrackReview).filter(TrackReview.track_id == track.id).order_by(TrackReview.created_at.desc()).all()

    audio_url = None
    try:
        if getattr(track, "submission_id", None):
            sub = db.session.get(TrackSubmission, int(track.submission_id))
            if sub and sub.status not in ("deleted", "failed"):
                ext = (sub.original_ext or "").lower().lstrip(".")
                if ext in ALLOWED_SUBMISSION_EXTS:
                    audio_url = url_for("submission_audio", file_uuid=sub.file_uuid, ext=ext)
    except Exception:
        audio_url = None

    player_title = None
    player_subtitle = None
    try:
        if getattr(track, "submission_id", None):
            sub = db.session.get(TrackSubmission, track.submission_id)
            if sub:
                player_title = sub.title
                player_subtitle = sub.artist
    except Exception:
        pass
    if not player_title:
        player_title = getattr(track, "name", "Прослушивание")

    active_awards = db.session.query(Award).filter(Award.status == "active").order_by(Award.created_at.desc()).all()
    award_noms = []
    award_wins = []
    try:
        award_noms = [
            {"title": r[0], "emoji": (r[1] or "🎖"), "kind": "nom"}
            for r in (
                db.session.query(Award.title, Award.icon_emoji)
                .join(AwardNomination, AwardNomination.award_id == Award.id)
                .filter(AwardNomination.track_id == track.id)
                .filter(Award.status.in_(["active", "ended"]))
                .order_by(Award.created_at.desc())
                .all()
            )
        ]
        award_wins = [
            {"title": r[0], "emoji": (r[1] or "🏆"), "kind": "win"}
            for r in (
                db.session.query(Award.title, Award.icon_emoji)
                .join(AwardNomination, Award.winner_nomination_id == AwardNomination.id)
                .filter(AwardNomination.track_id == track.id)
                .all()
            )
        ]
    except Exception:
        pass

    return render_template(
        "track.html",
        track=track,
        audio_url=audio_url,
        player_title=player_title,
        player_subtitle=player_subtitle,
        overall_avg=overall_avg,
        criteria_stats=criteria_stats,
        raters_stats=raters_stats,
        review_overall=review_overall,
        review_count=review_count,
        reviews=reviews,
        my_review=my_review,
        my_review_score_map=my_review_score_map,
        CRITERIA=CRITERIA,
        is_admin=is_admin,
        active_awards=active_awards,
        award_noms=award_noms,
        award_wins=award_wins,
    )


@app.route("/track/<int:track_id>/review", methods=["POST"])
def submit_review(track_id: int):
    """Create or update the current user's review for a track."""
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        flash("Трек не найден", "error")
        return redirect(url_for("top_tracks"))

    user = get_current_user()
    if not user:
        flash("Сначала войдите в аккаунт", "error")
        return redirect(url_for("login"))
    if not user.is_email_verified():
        flash("Подтвердите email, чтобы оставлять рецензии", "error")
        return redirect(url_for("settings_profile"))

    text = (request.form.get("text") or "").strip()

    scores: dict[str, int] = {}
    errors = []
    for key, _label in CRITERIA:
        v = request.form.get(f"score_{key}", type=int)
        if v is None:
            v = 0
        try:
            v = int(v)
        except Exception:
            v = 0
        if not (0 <= v <= 10):
            errors.append("Оценка по каждому параметру должна быть от 0 до 10")
            break
        scores[key] = v
    if not text:
        errors.append("Текст рецензии не может быть пустым")
    elif len(text) > 4000:
        errors.append("Текст слишком длинный (максимум 4000 символов)")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("track_page", track_id=track.id) + "#reviews")

    overall = round(sum(scores.values()) / max(1, len(scores)), 2)

    review = (
        db.session.query(TrackReview)
        .filter(TrackReview.track_id == track.id, TrackReview.user_id == user.id)
        .first()
    )
    if review:
        review.overall = float(overall)
        review.rating = int(round(overall))
        review.text = text
        existing = {s.criterion_key: s for s in (review.scores or [])}
        for k, v in scores.items():
            if k in existing:
                existing[k].score = v
            else:
                db.session.add(TrackReviewScore(review_id=review.id, criterion_key=k, score=v))
        flash("Рецензия обновлена", "success")
    else:
        review = TrackReview(track_id=track.id, user_id=user.id, rating=int(round(overall)), overall=float(overall), text=text)
        db.session.add(review)
        db.session.flush()
        for k, v in scores.items():
            db.session.add(TrackReviewScore(review_id=review.id, criterion_key=k, score=v))
        flash("Рецензия опубликована", "success")

    db.session.commit()
    return redirect(url_for("track_page", track_id=track.id) + "#reviews")


# -----------------
# Top Tracks
# -----------------

@app.route("/top")
def top_tracks():
    """Top tracks with sorting."""
    page = request.args.get("page", 1)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    sort_by = request.args.get("sort_by", "streamers")
    direction = request.args.get("direction", "desc")

    if sort_by not in ("streamers", "viewers"):
        sort_by = "streamers"
    if direction not in ("asc", "desc"):
        direction = "desc"

    per_page = 15
    offset = (page - 1) * per_page

    review_subq = (
        db.session.query(
            TrackReview.track_id.label("r_track_id"),
            func.avg(TrackReview.overall).label("avg_viewers"),
        )
        .group_by(TrackReview.track_id)
        .subquery()
    )

    base_query = (
        db.session.query(
            Track.id.label("track_id"),
            Track.name.label("track_name"),
            Track.created_at.label("created_at"),
            func.avg(Evaluation.score).label("avg_streamers"),
            review_subq.c.avg_viewers,
        )
        .join(Evaluation, Evaluation.track_id == Track.id)
        .outerjoin(review_subq, review_subq.c.r_track_id == Track.id)
        .filter(Track.is_deleted.is_(False))
        .group_by(Track.id, Track.name, Track.created_at, review_subq.c.avg_viewers)
    )

    if sort_by == "viewers":
        sort_col = review_subq.c.avg_viewers
    else:
        sort_col = func.avg(Evaluation.score)

    if direction == "asc":
        order_expr = sort_col.asc()
    else:
        order_expr = sort_col.desc()

    query = base_query.order_by(order_expr, Track.created_at.desc())

    rows = query.offset(offset).limit(per_page).all()
    total_tracks = db.session.query(func.count(Track.id)).filter(Track.is_deleted.is_(False)).scalar() or 0
    total_pages = max(1, (total_tracks + per_page - 1) // per_page)

    tracks = []
    for idx_row, row in enumerate(rows):
        tracks.append({
            "position": offset + idx_row + 1,
            "id": row.track_id,
            "name": row.track_name,
            "created_at": row.created_at,
            "avg_streamers": float(row.avg_streamers) if row.avg_streamers is not None else None,
            "avg_viewers": float(row.avg_viewers) if row.avg_viewers is not None else None,
        })

    track_ids = [t["id"] for t in tracks]
    active_awards = db.session.query(Award).filter(Award.status == "active").order_by(Award.created_at.desc()).all()

    nomination_map = {tid: [] for tid in track_ids}
    winner_map = {tid: [] for tid in track_ids}
    if track_ids:
        rows_nom = (
            db.session.query(AwardNomination.track_id, Award.title, Award.icon_emoji, Award.status)
            .join(Award, Award.id == AwardNomination.award_id)
            .filter(AwardNomination.track_id.in_(track_ids))
            .order_by(Award.created_at.desc())
            .all()
        )
        for tid, title, emoji, st in rows_nom:
            if st in ("active", "ended"):
                nomination_map.setdefault(tid, []).append({"title": title, "emoji": (emoji or "🎖"), "kind": "nom"})

        rows_win = (
            db.session.query(AwardNomination.track_id, Award.title, Award.icon_emoji)
            .join(Award, Award.winner_nomination_id == AwardNomination.id)
            .filter(AwardNomination.track_id.in_(track_ids))
            .all()
        )
        for tid, title, emoji in rows_win:
            winner_map.setdefault(tid, []).append({"title": title, "emoji": (emoji or "🏆"), "kind": "win"})

    for t in tracks:
        tid = t["id"]
        t["award_wins"] = winner_map.get(tid, [])
        t["award_noms"] = nomination_map.get(tid, [])

    return render_template(
        "top.html",
        tracks=tracks,
        page=page,
        total_pages=total_pages,
        sort_by=sort_by,
        direction=direction,
        is_admin=_require_admin(),
        active_awards=active_awards,
    )


# -----------------
# Viewers Page
# -----------------

@app.route("/viewers")
def viewers_page():
    """Публичная страница для зрителей."""
    page = request.args.get("page", 1)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    per_page = 15
    offset = (page - 1) * per_page

    query = Track.query.filter(Track.is_deleted.is_(False)).order_by(Track.created_at.desc(), Track.id.desc())
    total_tracks = query.count()
    tracks = query.offset(offset).limit(per_page).all()
    total_pages = max(1, (total_tracks + per_page - 1) // per_page)

    resp = make_response(
        render_template(
            "viewers.html",
            tracks=tracks,
            page=page,
            total_pages=total_pages,
            CRITERIA=CRITERIA,
        )
    )

    if not request.cookies.get(VIEWER_COOKIE_NAME):
        vid = _get_or_create_viewer_id()
        resp.set_cookie(
            VIEWER_COOKIE_NAME,
            vid,
            max_age=VIEWER_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )

    return resp
