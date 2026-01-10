"""JSON API routes for frontend and external integrations.

Migrated from the monolithic routes.py for better maintainability.
"""

from flask import request, jsonify
from sqlalchemy import func

from ..core import app, db, get_current_user, _get_or_create_viewer_id, _serialize_queue_state, _get_playback_snapshot
from ..extensions import CRITERIA, VIEWER_COOKIE_NAME
from ..models import (
    Award,
    AwardNomination,
    Evaluation,
    Track,
    TrackReview,
    TrackSubmission,
    ViewerRating,
)


# -----------------
# Queue API
# -----------------

@app.route("/api/queue")
def api_queue_state():
    """JSON для очереди."""
    payload = _serialize_queue_state(limit=200)
    payload["active"] = _get_playback_snapshot().get("active")
    return jsonify(payload)


# -----------------
# Track Summary API
# -----------------

@app.route("/api/track/<int:track_id>/summary")
def track_summary(track_id: int):
    """JSON-сводка по треку."""
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    overall_avg_val = (
        db.session.query(func.avg(Evaluation.score))
        .filter(Evaluation.track_id == track_id)
        .scalar()
    )

    criteria_rows = (
        db.session.query(
            Evaluation.criterion_key,
            func.avg(Evaluation.score).label("avg_score"),
        )
        .filter(Evaluation.track_id == track_id)
        .group_by(Evaluation.criterion_key)
        .all()
    )
    criteria = []
    for key, label in CRITERIA:
        row = next((r for r in criteria_rows if r.criterion_key == key), None)
        avg_val = float(row.avg_score) if row and row.avg_score is not None else None
        criteria.append({"key": key, "label": label, "avg": avg_val})

    rater_rows = (
        db.session.query(
            Evaluation.rater_name,
            func.avg(Evaluation.score).label("avg_score"),
        )
        .filter(Evaluation.track_id == track_id)
        .group_by(Evaluation.rater_name)
        .order_by(Evaluation.rater_name)
        .all()
    )
    raters = [{"name": row.rater_name, "avg": float(row.avg_score)} for row in rater_rows]

    viewer_overall_val = (
        db.session.query(func.avg(TrackReview.overall))
        .filter(TrackReview.track_id == track_id)
        .scalar()
    )
    review_count = (
        db.session.query(func.count(TrackReview.id))
        .filter(TrackReview.track_id == track_id)
        .scalar()
        or 0
    )

    payload = {
        "track": {
            "id": track.id,
            "name": track.name,
            "created_at": track.created_at.isoformat() if track.created_at else None,
        },
        "overall_avg": float(overall_avg_val) if overall_avg_val is not None else None,
        "criteria": criteria,
        "raters": raters,
        "viewer_overall_avg": float(viewer_overall_val) if viewer_overall_val is not None else None,
        "viewer_criteria": [],
        "review_count": int(review_count),
    }
    return jsonify(payload)


# -----------------
# Viewer Rating API
# -----------------

@app.route("/api/viewers/track/<int:track_id>")
def viewer_track_summary(track_id: int):
    """JSON для модалки зрителя."""
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    viewer_id = request.cookies.get(VIEWER_COOKIE_NAME)

    has_voted = False
    viewer_scores = {}
    if viewer_id:
        rows = ViewerRating.query.filter_by(viewer_id=viewer_id, track_id=track_id).all()
        if rows:
            has_voted = True
            for r in rows:
                viewer_scores[r.criterion_key] = r.score

    crit_rows = (
        db.session.query(
            ViewerRating.criterion_key,
            func.avg(ViewerRating.score).label("avg_score"),
        )
        .filter(ViewerRating.track_id == track_id)
        .group_by(ViewerRating.criterion_key)
        .all()
    )

    criteria_stats = []
    for key, label in CRITERIA:
        row = next((r for r in crit_rows if r.criterion_key == key), None)
        avg_val = float(row.avg_score) if row else 0.0
        criteria_stats.append({"key": key, "label": label, "avg_score": avg_val})

    overall_avg = (
        db.session.query(func.avg(ViewerRating.score))
        .filter(ViewerRating.track_id == track_id)
        .scalar()
        or 0.0
    )

    return jsonify({
        "track": {
            "id": track.id,
            "name": track.name,
            "created_at": track.created_at.isoformat() if track.created_at else None,
        },
        "viewer": {
            "has_voted": has_voted,
            "scores": viewer_scores,
        },
        "criteria": criteria_stats,
        "overall_avg": float(overall_avg),
    })


@app.route("/api/viewers/rate", methods=["POST"])
def viewers_rate():
    """Принять оценки от зрителя."""
    data = request.get_json(silent=True) or {}
    track_id = data.get("track_id")
    ratings = data.get("ratings") or {}

    if not isinstance(track_id, int):
        return jsonify({"error": "bad_track_id"}), 400

    track = db.session.get(Track, track_id)
    if not track:
        return jsonify({"error": "track_not_found"}), 404

    viewer_id = request.cookies.get(VIEWER_COOKIE_NAME)
    if not viewer_id:
        viewer_id = _get_or_create_viewer_id()

    existing_count = ViewerRating.query.filter_by(viewer_id=viewer_id, track_id=track_id).count()
    if existing_count > 0:
        return jsonify({"error": "already_rated"}), 400

    criterion_keys = {key for key, _ in CRITERIA}
    new_rows = []
    for key, val in (ratings or {}).items():
        if key not in criterion_keys:
            continue
        try:
            score = int(val)
        except (TypeError, ValueError):
            continue
        score = max(0, min(10, score))
        new_rows.append(
            ViewerRating(
                viewer_id=viewer_id,
                track_id=track_id,
                criterion_key=key,
                score=score,
            )
        )

    if not new_rows:
        return jsonify({"error": "no_valid_scores"}), 400

    db.session.add_all(new_rows)
    db.session.commit()

    overall_avg = (
        db.session.query(func.avg(ViewerRating.score))
        .filter(ViewerRating.track_id == track_id)
        .scalar()
        or 0.0
    )

    return jsonify({"status": "ok", "overall_avg": float(overall_avg)})
