"""Admin routes: admin panel, news CRUD, stream config, user management.

Migrated from the monolithic routes.py for better maintainability.
"""

import io
import os
import uuid
from uuid import uuid4

import qrcode
from flask import request, redirect, url_for, flash, render_template, make_response

from ..core import (
    app, db, get_current_user,
    _init_default_raters,
    _get_track_url,
    _is_image_filename,
    _require_admin,
    _require_panel_access,
    _require_superadmin,
    sanitize_news_html,
)
from ..extensions import (
    NEWS_UPLOAD_DIR,
    UPLOAD_DIR,
    secure_filename,
)
from ..models import (
    News,
    NewsAttachment,
    StreamConfig,
    Track,
    TrackComment,
    TrackSubmission,
    User,
)
from ..state import _serialize_state, _broadcast_queue_state


# -----------------
# Admin Panel
# -----------------

@app.route("/panel")
def index():
    if not _require_panel_access():
        return redirect(url_for("login"))
    _init_default_raters()
    initial_state = _serialize_state()
    return render_template(
        "index.html",
        initial_state=initial_state,
        is_admin=_require_admin(),
    )


@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Admin panel for ANTIGAZ."""
    if not _require_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        form_type = request.form.get("form") or "news"

        # User management (superadmin only)
        if form_type == "users":
            if not _require_superadmin():
                flash("Только главный админ может управлять админ-аккаунтами", "error")
                return redirect(url_for("admin", tab="users"))

            action = request.form.get("action") or "create"
            username = (request.form.get("username") or "").strip()
            password = (request.form.get("password") or "").strip()
            role = (request.form.get("role") or "").strip() or "admin"

            if action == "create":
                if not username or not password:
                    flash("Логин и пароль не могут быть пустыми", "error")
                    return redirect(url_for("admin", tab="users"))
                existing = db.session.query(User).filter_by(username=username).first()
                if existing:
                    flash("Пользователь с таким логином уже существует", "error")
                    return redirect(url_for("admin", tab="users"))

                if role not in ("admin", "superadmin", "judge"):
                    role = "admin"

                user = User(username=username, password=password, role=role)
                db.session.add(user)
                db.session.commit()
                flash("Админ успешно создан", "success")
                return redirect(url_for("admin", tab="users"))

            elif action == "delete":
                user_id = request.form.get("user_id", type=int)
                if not user_id:
                    flash("Не указан пользователь для удаления", "error")
                    return redirect(url_for("admin", tab="users"))

                user = db.session.get(User, user_id)
                if not user:
                    flash("Пользователь не найден", "error")
                    return redirect(url_for("admin", tab="users"))

                current = get_current_user()
                if user.is_superadmin():
                    flash("Нельзя удалить главного админа", "error")
                    return redirect(url_for("admin", tab="users"))
                if current and current.id == user.id:
                    flash("Нельзя удалить самого себя", "error")
                    return redirect(url_for("admin", tab="users"))

                db.session.delete(user)
                db.session.commit()
                flash("Админ удалён", "success")
                return redirect(url_for("admin", tab="users"))

            elif action == "update_role":
                user_id = request.form.get("user_id", type=int)
                new_role = (request.form.get("role") or "").strip() or "user"
                if not user_id:
                    flash("Не указан пользователь", "error")
                    return redirect(url_for("admin", tab="users"))

                user = db.session.get(User, user_id)
                if not user:
                    flash("Пользователь не найден", "error")
                    return redirect(url_for("admin", tab="users"))

                allowed_roles = {"user", "judge", "admin", "superadmin"}
                if new_role not in allowed_roles:
                    flash("Некорректная роль", "error")
                    return redirect(url_for("admin", tab="users"))

                current = get_current_user()

                if current and current.id == user.id and new_role != user.role:
                    flash("Нельзя менять роль самому себе", "error")
                    return redirect(url_for("admin", tab="users"))

                if user.is_superadmin() and new_role != "superadmin":
                    flash("Нельзя менять роль главного админа", "error")
                    return redirect(url_for("admin", tab="users"))

                user.role = new_role
                db.session.commit()
                flash("Роль обновлена", "success")
                return redirect(url_for("admin", tab="users"))

        # Comment moderation
        if form_type == "comments":
            comment_id = request.form.get("comment_id", type=int)
            action = (request.form.get("action") or "").strip()
            if not comment_id:
                flash("Не указан комментарий", "error")
                return redirect(url_for("admin", tab="comments"))

            comment = db.session.get(TrackComment, comment_id)
            if not comment or comment.is_deleted:
                flash("Комментарий не найден", "error")
                return redirect(url_for("admin", tab="comments"))

            if action == "approve":
                comment.is_approved = True
                db.session.commit()
                flash("Комментарий одобрен", "success")
            elif action == "delete":
                comment.is_deleted = True
                db.session.commit()
                flash("Комментарий скрыт", "success")
            else:
                flash("Неизвестное действие над комментарием", "error")

            return redirect(url_for("admin", tab="comments"))

        # News creation
        if form_type == "news":
            title = (request.form.get("title") or "").strip()
            tag = (request.form.get("tag") or "").strip() or None

            raw_html = request.form.get("text_html")
            if raw_html is None:
                raw_html = request.form.get("text") or ""
            text_html = sanitize_news_html(raw_html)

            if not title:
                flash("Заголовок не может быть пустым", "error")
            else:
                news = News(title=title, text=text_html or None, tag=tag)
                db.session.add(news)
                db.session.commit()

                files = request.files.getlist("attachments")
                if not files:
                    legacy = request.files.get("attachment")
                    files = [legacy] if legacy else []

                for f in files or []:
                    if not f or not getattr(f, "filename", ""):
                        continue
                    original = f.filename
                    safe = secure_filename(original)
                    if not safe:
                        continue
                    stored = f"news_{news.id}_{uuid.uuid4().hex}_{safe}"
                    try:
                        f.save(os.path.join(NEWS_UPLOAD_DIR, stored))
                        db.session.add(
                            NewsAttachment(
                                news_id=news.id,
                                stored_filename=stored,
                                original_filename=original,
                            )
                        )
                    except Exception as e:
                        print("Failed to save attachment for news", news.id, e)
                        flash("Новость добавлена, но вложение сохранить не удалось", "warning")

                db.session.commit()
                flash("Новость добавлена", "success")
                return redirect(url_for("admin", tab="news"))

            return redirect(url_for("admin", tab="news"))

        # Stream config
        elif form_type == "stream":
            cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
            if not cfg:
                cfg = StreamConfig()
                db.session.add(cfg)

            if cfg.is_active:
                cfg.is_active = False
                cfg.title = ""
                cfg.url = ""
                db.session.commit()
                flash("Стрим завершён", "success")
                return redirect(url_for("admin", tab="stream"))

            title = (request.form.get("stream_title") or "").strip()
            url = (request.form.get("stream_url") or "").strip()

            if not url:
                flash("Чтобы начать стрим, укажи ссылку на стрим", "error")
                return redirect(url_for("admin", tab="stream"))

            cfg.title = title or ""
            cfg.url = url
            cfg.is_active = True

            db.session.commit()
            flash("Стрим запущен", "success")
            return redirect(url_for("admin", tab="stream"))

        # OBS Widget token
        elif form_type == "stream_widget":
            cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
            if not cfg:
                cfg = StreamConfig()
                db.session.add(cfg)

            cfg.widget_token = uuid4().hex
            db.session.commit()
            flash("Ссылка для OBS-виджета обновлена", "success")
            return redirect(url_for("admin", tab="stream"))

    # GET request - render admin page
    comments_rows = (
        db.session.query(TrackComment, Track)
        .join(Track, Track.id == TrackComment.track_id)
        .filter(TrackComment.is_deleted.is_(False))
        .order_by(TrackComment.is_approved.asc(), TrackComment.created_at.desc())
        .limit(100)
        .all()
    )

    page = request.args.get("page", 1, type=int)
    per_page = 5

    news_query = db.session.query(News).order_by(News.created_at.desc())
    news_pagination = news_query.paginate(page=page, per_page=per_page, error_out=False)
    news_list = news_pagination.items

    news_ids = [n.id for n in news_list]
    attachments = {nid: [] for nid in news_ids}

    if news_ids:
        try:
            rows = (
                db.session.query(NewsAttachment)
                .filter(NewsAttachment.news_id.in_(news_ids))
                .order_by(NewsAttachment.uploaded_at.asc())
                .all()
            )
            for att in rows:
                attachments.setdefault(att.news_id, []).append({
                    "id": att.id,
                    "stored": att.stored_filename,
                    "original": att.original_filename,
                    "is_image": _is_image_filename(att.stored_filename),
                    "url": url_for("static", filename="uploads/news/" + att.stored_filename),
                })
        except Exception:
            pass

    try:
        filenames = os.listdir(UPLOAD_DIR)
    except FileNotFoundError:
        filenames = []

    for n in news_list:
        if attachments.get(n.id):
            continue
        prefix = f"news_{n.id}_"
        for fname in filenames:
            if fname.startswith(prefix):
                attachments[n.id] = [{
                    "id": None,
                    "stored": fname,
                    "original": fname.replace(prefix, "", 1) or fname,
                    "is_image": _is_image_filename(fname),
                    "url": url_for("static", filename="uploads/" + fname),
                }]
                break

    current_user = get_current_user()

    active_tab = request.args.get("tab") or "news"
    if active_tab not in ("news", "stream", "users", "comments"):
        active_tab = "news"
    if active_tab == "users" and not (current_user and current_user.is_superadmin()):
        active_tab = "news"

    cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()

    widget_url = None
    if cfg and cfg.widget_token:
        widget_url = url_for("obs_widget", token=cfg.widget_token, _external=True)

    users = []
    if current_user and current_user.is_superadmin():
        users = db.session.query(User).order_by(User.created_at.asc()).all()

    return render_template(
        "admin.html",
        news_list=news_list,
        news_pagination=news_pagination,
        stream_config=cfg,
        attachments=attachments,
        active_tab=active_tab,
        current_user=current_user,
        users=users,
        track_comments=comments_rows,
        widget_url=widget_url,
    )


# -----------------
# News CRUD
# -----------------

@app.route("/admin/news/<int:news_id>/delete", methods=["POST"])
def delete_news(news_id):
    if not _require_admin():
        return redirect(url_for("login"))

    news = db.session.get(News, news_id)
    if not news:
        flash("Новость не найдена", "error")
        return redirect(url_for("admin"))

    try:
        for att in list(getattr(news, "attachments", []) or []):
            try:
                file_path = os.path.join(NEWS_UPLOAD_DIR, att.stored_filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except Exception:
                pass
    except Exception:
        pass

    try:
        for fname in os.listdir(UPLOAD_DIR):
            if fname.startswith(f"news_{news.id}_"):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fname))
                except Exception:
                    pass
    except Exception:
        pass

    db.session.delete(news)
    db.session.commit()
    flash("Новость удалена", "success")
    return redirect(url_for("admin"))


@app.route("/admin/news/new", methods=["GET", "POST"])
def news_new():
    if not _require_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        tag = (request.form.get("tag") or "").strip() or None
        raw_html = request.form.get("text_html") or ""
        text_html = sanitize_news_html(raw_html)

        if not title:
            flash("Заголовок не может быть пустым", "error")
            return redirect(url_for("news_new"))

        news = News(title=title, text=text_html or None, tag=tag)
        db.session.add(news)
        db.session.commit()

        files = request.files.getlist("attachments")
        for f in files or []:
            if not f or not getattr(f, "filename", ""):
                continue
            original = f.filename
            safe = secure_filename(original)
            if not safe:
                continue
            stored = f"news_{news.id}_{uuid.uuid4().hex}_{safe}"
            try:
                f.save(os.path.join(NEWS_UPLOAD_DIR, stored))
                db.session.add(
                    NewsAttachment(
                        news_id=news.id,
                        stored_filename=stored,
                        original_filename=original,
                    )
                )
            except Exception as e:
                print("Failed to save news attachment:", e)

        db.session.commit()
        flash("Новость добавлена", "success")
        return redirect(url_for("admin", tab="news"))

    return render_template("news_edit.html", mode="create", news=None)


@app.route("/admin/news/<int:news_id>/edit", methods=["GET", "POST"])
def news_edit(news_id: int):
    if not _require_admin():
        return redirect(url_for("login"))

    news = db.session.get(News, news_id)
    if not news:
        flash("Новость не найдена", "error")
        return redirect(url_for("admin", tab="news"))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        tag = (request.form.get("tag") or "").strip() or None
        raw_html = request.form.get("text_html") or ""
        text_html = sanitize_news_html(raw_html)

        if not title:
            flash("Заголовок не может быть пустым", "error")
            return redirect(url_for("news_edit", news_id=news_id))

        news.title = title
        news.tag = tag
        news.text = text_html or None
        db.session.commit()

        files = request.files.getlist("attachments")
        for f in files or []:
            if not f or not getattr(f, "filename", ""):
                continue
            original = f.filename
            safe = secure_filename(original)
            if not safe:
                continue
            stored = f"news_{news.id}_{uuid.uuid4().hex}_{safe}"
            try:
                f.save(os.path.join(NEWS_UPLOAD_DIR, stored))
                db.session.add(
                    NewsAttachment(
                        news_id=news.id,
                        stored_filename=stored,
                        original_filename=original,
                    )
                )
            except Exception as e:
                print("Failed to save news attachment:", e)

        db.session.commit()
        flash("Новость обновлена", "success")
        return redirect(url_for("admin", tab="news"))

    attachments = []
    try:
        for att in getattr(news, "attachments", []) or []:
            attachments.append({
                "id": att.id,
                "stored": att.stored_filename,
                "original": att.original_filename,
                "is_image": _is_image_filename(att.stored_filename),
                "url": url_for("static", filename="uploads/news/" + att.stored_filename),
            })
    except Exception:
        pass

    if not attachments:
        try:
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith(f"news_{news.id}_"):
                    attachments.append({
                        "id": None,
                        "stored": fname,
                        "original": fname.replace(f"news_{news.id}_", "", 1) or fname,
                        "is_image": _is_image_filename(fname),
                        "url": url_for("static", filename="uploads/" + fname),
                    })
        except Exception:
            pass

    return render_template("news_edit.html", mode="edit", news=news, attachments=attachments)


@app.route("/admin/news/attachment/<int:attachment_id>/delete", methods=["POST"])
def news_attachment_delete(attachment_id: int):
    if not _require_admin():
        return redirect(url_for("login"))

    att = db.session.get(NewsAttachment, attachment_id)
    if not att:
        flash("Вложение не найдено", "error")
        return redirect(url_for("admin", tab="news"))

    news_id = att.news_id
    try:
        file_path = os.path.join(NEWS_UPLOAD_DIR, att.stored_filename)
        if os.path.isfile(file_path):
            os.remove(file_path)
    except Exception:
        pass

    db.session.delete(att)
    db.session.commit()
    flash("Вложение удалено", "success")
    return redirect(url_for("news_edit", news_id=news_id))


# -----------------
# Admin Actions
# -----------------

@app.route("/admin/clear_queue", methods=["POST"])
def admin_clear_queue():
    if not _require_admin():
        return redirect(url_for("login"))

    if (request.form.get("confirm") or "").strip() != "yes":
        flash("Очистка очереди отменена.", "warning")
        return redirect(url_for("index"))

    cleared = (
        db.session.query(TrackSubmission)
        .filter(TrackSubmission.status == "queued")
        .update(
            {TrackSubmission.status: "deleted"},
            synchronize_session=False
        )
    )
    db.session.commit()

    flash(f"Очередь очищена: {cleared} трек(ов).", "success")

    try:
        _broadcast_queue_state()
    except Exception:
        pass

    return redirect(url_for("index"))


@app.route("/admin/tracks/<int:track_id>/rename", methods=["POST"])
def admin_rename_track(track_id: int):
    if not _require_admin():
        from flask import jsonify
        return jsonify({"error": "forbidden"}), 403

    from flask import jsonify
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "empty_name"}), 400

    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    track.name = new_name
    db.session.commit()
    return jsonify({"success": True, "id": track.id, "name": track.name})


@app.route("/admin/tracks/<int:track_id>/delete", methods=["POST"])
def admin_delete_track(track_id: int):
    if not _require_admin():
        from flask import jsonify
        return jsonify({"error": "forbidden"}), 403

    from flask import jsonify
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    track.is_deleted = True
    db.session.commit()
    return jsonify({"success": True})


# -----------------
# QR & OBS Widget
# -----------------

@app.route("/qr/track/<int:track_id>.png")
def qr_for_track(track_id: int):
    track = db.session.get(Track, track_id)
    if not track or getattr(track, "is_deleted", False):
        return make_response("Track not found", 404)

    track_url = _get_track_url(track_id)

    img = qrcode.make(track_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/widget/<string:token>")
def obs_widget(token: str):
    """OBS widget accessible by widget_token."""
    cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
    if not cfg or not cfg.widget_token or cfg.widget_token != token:
        return make_response("Widget is not configured", 404)

    return render_template("obs_widget.html")


# -----------------
# Superadmin Track Upload (for testing)
# -----------------

from datetime import datetime
from ..extensions import ALLOWED_SUBMISSION_EXTS, SUBMISSIONS_RAW_DIR


@app.route("/admin/upload_track", methods=["GET", "POST"])
def admin_upload_track():
    """Upload track directly to queue (superadmin only, for local testing)."""
    if not _require_superadmin():
        flash("Только главный админ может загружать треки через сайт", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        artist = (request.form.get("artist") or "").strip()
        title = (request.form.get("title") or "").strip()
        priority = request.form.get("priority", type=int) or 0
        file = request.files.get("file")

        # Validation
        if not artist and not title:
            flash("Укажите исполнителя или название", "error")
            return redirect(url_for("admin_upload_track"))

        if not file or not file.filename:
            flash("Выберите аудиофайл", "error")
            return redirect(url_for("admin_upload_track"))

        # Get extension
        original_filename = file.filename
        ext = ""
        if "." in original_filename:
            ext = original_filename.rsplit(".", 1)[-1].lower()

        if ext not in ALLOWED_SUBMISSION_EXTS:
            flash(f"Неподдерживаемый формат. Разрешены: {', '.join(sorted(ALLOWED_SUBMISSION_EXTS))}", "error")
            return redirect(url_for("admin_upload_track"))

        # Generate UUID and save file
        file_uuid = uuid4().hex
        raw_filename = f"{file_uuid}.{ext}"
        raw_path = os.path.join(SUBMISSIONS_RAW_DIR, raw_filename)

        try:
            os.makedirs(SUBMISSIONS_RAW_DIR, exist_ok=True)
            file.save(raw_path)
        except Exception as e:
            flash(f"Ошибка сохранения файла: {e}", "error")
            return redirect(url_for("admin_upload_track"))

        # Validate priority
        if priority not in (0, 100, 200, 300, 400):
            priority = 0

        # Create submission
        sub = TrackSubmission(
            artist=artist or "",
            title=title or "",
            priority=priority,
            status="queued",
            file_uuid=file_uuid,
            original_filename=original_filename,
            original_ext=ext,
            created_at=datetime.utcnow(),
            priority_set_at=datetime.utcnow(),
            tg_user_id=None,
            tg_username=None,
            payment_status="none" if priority == 0 else "paid",
            payment_provider="admin_upload" if priority > 0 else None,
            payment_amount=priority if priority > 0 else None,
        )
        db.session.add(sub)
        db.session.commit()

        # Broadcast queue update
        try:
            _broadcast_queue_state()
        except Exception:
            pass

        flash(f"Трек «{artist} — {title}» добавлен в очередь", "success")
        return redirect(url_for("index"))

    # GET - show upload form
    return render_template("admin_upload_track.html")

