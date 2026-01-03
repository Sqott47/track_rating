"""HTTP routes for TrackRaterAntigaz (split from monolithic app.py).

Why explicit imports below?
`from .core import *` does **not** import names that start with `_`.
We still call several internal helpers (e.g. `_init_default_raters`) from
the legacy code, so we import them explicitly to keep behaviour identical.
"""

from .core import *  # noqa: F401,F403

# Explicitly import underscore-prefixed helpers used in this module.
from .core import (
    _get_or_create_viewer_id,
    _get_playback_snapshot,
    _get_s3_client,
    _get_track_url,
    _init_default_raters,
    _is_image_filename,
    _is_safe_uuid,
    _require_admin,
    _require_panel_access,
    _require_superadmin,
    _s3_is_configured,
    _s3_key_for_submission,
    _serialize_queue_state,
    _submission_display_name,
)
from .state import _serialize_state, _broadcast_queue_state
from .models import EmailVerificationToken, PasswordResetToken, TrackReview, TrackReviewScore
from .donationalerts import build_authorize_url, exchange_code_for_tokens, save_tokens, load_tokens, fetch_user_oauth

from datetime import datetime, timedelta
import re

from .mailer import generate_token, sha256_hex, resend_send_email


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # сначала пробуем найти пользователя в БД
        user = None
        if username:
            user = db.session.query(User).filter_by(username=username).first()

        if user and user.check_password(password):
            # If this account still has a legacy plaintext password, upgrade it
            # in-place on successful login.
            try:
                if not user.has_password_hash():
                    user.set_password(password)
                    db.session.add(user)
                    db.session.commit()
            except Exception as e:
                # Login should still work even if upgrade fails.
                print("Warning: could not upgrade password hash:", e)

            session["user"] = user.username
            session["role"] = user.role
            session["session_version"] = int(user.session_version or 1)
            _init_default_raters()
            # Judges/admins go to the panel, regular users go to the public home.
            if user.is_judge():
                return redirect(url_for("index"))
            return redirect(url_for("top_tracks"))

        # fallback на старый режим единственного админа из ENV
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            # Ensure there is a matching DB user so profile/security features work.
            try:
                db_user = db.session.query(User).filter_by(username=ADMIN_USERNAME).first()
                if not db_user:
                    db_user = User(username=ADMIN_USERNAME, role="superadmin")
                    db_user.set_password(ADMIN_PASSWORD)
                    db.session.add(db_user)
                    db.session.commit()
                elif not db_user.check_password(ADMIN_PASSWORD):
                    # Keep DB in sync with ENV credentials.
                    db_user.set_password(ADMIN_PASSWORD)
                    if db_user.role != "superadmin":
                        db_user.role = "superadmin"
                    db.session.add(db_user)
                    db.session.commit()
            except Exception as e:
                print("Warning: could not sync ENV admin to DB:", e)
                db_user = None

            session["user"] = ADMIN_USERNAME
            session["role"] = "superadmin"
            session["session_version"] = int((db_user.session_version if db_user else 1) or 1)
            _init_default_raters()
            return redirect(url_for("index"))

        flash("Неверный логин или пароль", "error")
        # With Turbo form submissions, returning HTML directly on POST can
        # lead to scripts not being executed reliably. Use PRG.
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Open registration (outside Turbo).

    Flow:
    - create user (role=user, email unverified)
    - log user in
    - send verification email
    """
    errors = {}
    form = {
        "username": (request.form.get("username") or "").strip(),
        "email": (request.form.get("email") or "").strip().lower(),
    }

    if request.method == "POST":
        username = form["username"]
        email = form["email"]
        p1 = request.form.get("password") or ""
        p2 = request.form.get("password2") or ""

        # username: 3-20, латиница/цифры/_
        if not username:
            errors["username"] = "Логин обязателен"
        elif not re.fullmatch(r"[a-zA-Z0-9_]{3,20}", username):
            errors["username"] = "Логин: 3–20 символов, только латиница/цифры/_"
        else:
            if db.session.query(User).filter_by(username=username).first():
                errors["username"] = "Этот логин уже занят"

        # email
        if not email:
            errors["email"] = "Email обязателен"
        elif "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Неверный формат email"
        else:
            if db.session.query(User).filter_by(email=email).first():
                errors["email"] = "Этот email уже используется"

        # password
        if not p1 or len(p1) < 8:
            errors["password"] = "Минимум 8 символов"
        elif p1 != p2:
            errors["password2"] = "Пароли не совпадают"

        if not errors:
            user = User(username=username, email=email, role="user")
            user.set_password(p1)
            user.email_verified_at = None
            db.session.add(user)
            db.session.commit()

            # login
            session["user"] = user.username
            session["role"] = user.role
            session["session_version"] = int(user.session_version or 1)

            _send_verification_email(user)
            flash("Аккаунт создан! Мы отправили письмо для подтверждения email.", "success")
            return redirect(url_for("top_tracks"))

        # PRG
        flash("Исправьте ошибки в форме", "error")
        return render_template("register.html", errors=errors, form=form), 422

    return render_template("register.html", errors=errors, form=form)



@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    # Public page: always respond with success message to avoid email enumeration
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email:
            user = db.session.query(User).filter_by(email=email).first()
            if user and user.email and user.is_email_verified():
                _send_password_reset_email(user)
        flash("Если такой email существует, мы отправили инструкцию по восстановлению пароля.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("Некорректная ссылка", "error")
        return redirect(url_for("login"))

    token_hash = sha256_hex(token)
    row = (
        db.session.query(PasswordResetToken)
        .filter_by(token_hash=token_hash)
        .order_by(PasswordResetToken.id.desc())
        .first()
    )
    if not row or not row.is_valid():
        flash("Ссылка восстановления истекла или уже использована.", "error")
        return redirect(url_for("login"))

    user = db.session.query(User).get(row.user_id)
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("login"))

    errors = {}
    if request.method == "POST":
        p1 = request.form.get("password", "")
        p2 = request.form.get("password2", "")

        if not p1 or len(p1) < 8:
            errors["password"] = "Минимум 8 символов"
        elif p1 != p2:
            errors["password2"] = "Пароли не совпадают"

        if not errors:
            row.used_at = datetime.utcnow()
            user.set_password(p1)
            # Invalidate other sessions
            user.session_version = int(user.session_version or 1) + 1
            db.session.add(row)
            db.session.add(user)
            db.session.commit()
            flash("Пароль обновлён. Теперь можно войти.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", errors=errors)


def _send_password_reset_email(user: User) -> None:
    raw = generate_token()
    token_hash = sha256_hex(raw)
    expires_at = datetime.utcnow() + timedelta(hours=1)

    db.session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.session.commit()

    link = url_for("reset_password", token=raw, _external=True)
    html = render_template("emails/reset_password.html", user=user, link=link)

    ok, msg = resend_send_email(
        to_email=user.email,
        subject="Восстановление пароля",
        html=html,
        text=f"Сбросить пароль: {link}",
    )
    if not ok:
        app.logger.warning("Could not send password reset email: %s", msg)

@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("role", None)
    session.pop("session_version", None)
    return redirect(url_for("login"))


# -----------------
# Profile / Settings
# -----------------

def _require_login_or_redirect():
    u = get_current_user()
    if not u:
        return None, redirect(url_for("login"))
    return u, None


@app.route("/settings", methods=["GET"])
def settings_root():
    """Small convenience: redirect to profile settings."""
    u, resp = _require_login_or_redirect()
    if resp:
        return resp
    return redirect(url_for("settings_profile"))


@app.route("/settings/profile", methods=["GET", "POST"])
def settings_profile():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp

    errors = {}
    if request.method == "POST":
        display_name = (request.form.get("display_name") or "").strip()

        if not display_name:
            errors["display_name"] = "Имя не может быть пустым"
        elif len(display_name) < 2:
            errors["display_name"] = "Минимум 2 символа"
        elif len(display_name) > 30:
            errors["display_name"] = "Максимум 30 символов"

        if not errors:
            current_user.display_name = display_name
            db.session.add(current_user)
            db.session.commit()
            flash("Профиль обновлён", "success")
            return redirect(url_for("settings_profile"))

    return render_template(
        "settings_profile.html",
        current_user=current_user,
        errors=errors,
    ), (422 if errors and request.method == "POST" else 200)


@app.route("/settings/email", methods=["POST"])
def settings_email():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp

    email = (request.form.get("email") or "").strip().lower()
    errors = {}

    if not email:
        errors["email"] = "Email не может быть пустым"
    elif "@" not in email or "." not in email.split("@")[-1]:
        errors["email"] = "Неверный формат email"

    # Unique constraint check
    if not errors:
        exists = db.session.query(User).filter(User.email == email, User.id != current_user.id).first()
        if exists:
            errors["email"] = "Этот email уже используется"

    if errors:
        return (
            render_template(
                "settings_profile.html",
                current_user=current_user,
                errors=errors,
                email_form_value=email,
            ),
            422,
        )

    # Save email and mark as unverified if changed
    changed = (current_user.email or "").lower() != email
    current_user.email = email
    if changed:
        current_user.email_verified_at = None
    db.session.add(current_user)
    db.session.commit()

    # If the email is already verified and the user didn't change it, don't pretend
    # we sent anything (and don't spam).
    if (not changed) and current_user.is_email_verified():
        flash("Email сохранён. Он уже подтверждён ✅", "info")
        return redirect(url_for("settings_profile"))

    _send_verification_email(current_user)

    flash("Мы отправили письмо для подтверждения email", "success")
    return redirect(url_for("settings_profile"))


@app.route("/settings/email/resend", methods=["POST"])
def settings_email_resend():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp
    if not current_user.email:
        flash("Сначала укажите email", "error")
        return redirect(url_for("settings_profile"))
    if current_user.is_email_verified():
        flash("Email уже подтверждён", "success")
        return redirect(url_for("settings_profile"))

    _send_verification_email(current_user)
    flash("Письмо для подтверждения отправлено ещё раз", "success")
    return redirect(url_for("settings_profile"))


@app.route("/verify-email")
def verify_email():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("Некорректная ссылка", "error")
        return redirect(url_for("index"))

    token_hash = sha256_hex(token)
    row = (
        db.session.query(EmailVerificationToken)
        .filter_by(token_hash=token_hash)
        .order_by(EmailVerificationToken.id.desc())
        .first()
    )
    if not row:
        flash("Ссылка подтверждения недействительна", "error")
        return redirect(url_for("index"))

    # One-time + idempotent behaviour:
    # - already used token -> don't do anything
    # - expired token -> don't do anything
    if row.used_at is not None:
        flash("Эта ссылка уже была использована", "info")
        return redirect(url_for("settings_profile")) if session.get("user") else redirect(url_for("login"))

    if not row.is_valid():
        flash("Ссылка подтверждения истекла или уже использована", "error")
        return redirect(url_for("settings_profile")) if session.get("user") else redirect(url_for("login"))

    user = db.session.query(User).get(row.user_id)
    if not user or not user.email:
        flash("Пользователь не найден", "error")
        return redirect(url_for("index"))

    # If email is already verified, just burn this token and exit.
    if user.email_verified_at is not None:
        row.used_at = datetime.utcnow()
        db.session.add(row)
        db.session.commit()
        flash("Email уже подтверждён", "info")
        return redirect(url_for("settings_profile")) if session.get("user") else redirect(url_for("login"))

    now = datetime.utcnow()
    row.used_at = now
    user.email_verified_at = now

    # Burn all other outstanding verification tokens for this user
    try:
        db.session.query(EmailVerificationToken).filter(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.id != row.id,
        ).update({EmailVerificationToken.used_at: now})
    except Exception:
        pass
    db.session.add(row)
    db.session.add(user)
    db.session.commit()

    flash("Email подтверждён ✅", "success")
    # If user is logged in, refresh settings page; otherwise go to login
    if session.get("user"):
        return redirect(url_for("settings_profile"))
    return redirect(url_for("login"))


def _send_verification_email(user: User) -> None:
    # Don't spam / don't re-verify
    if user.email_verified_at is not None:
        return

    # simple rate-limit: 1 email / 60s
    try:
        last = (
            db.session.query(EmailVerificationToken)
            .filter_by(user_id=user.id)
            .order_by(EmailVerificationToken.id.desc())
            .first()
        )
        if last and last.created_at and (datetime.utcnow() - last.created_at).total_seconds() < 60:
            return
    except Exception:
        pass

    # Generate one-time token (store only its hash)
    raw = generate_token()
    token_hash = sha256_hex(raw)
    expires_at = datetime.utcnow() + timedelta(hours=1)

    db.session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.session.commit()

    link = url_for("verify_email", token=raw, _external=True)
    html = render_template("emails/verify_email.html", user=user, link=link)

    ok, msg = resend_send_email(
        to_email=user.email,
        subject="Подтвердите ваш email",
        html=html,
        text=f"Подтвердите email по ссылке: {link}",
    )
    if not ok:
        # Don't break UX; log and show a generic flash later if needed
        app.logger.warning("Could not send verification email: %s", msg)


@app.route("/settings/security", methods=["GET"])
def settings_security():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp
    return render_template("settings_security.html", current_user=current_user)


@app.route("/settings/security/password", methods=["POST"])
def settings_change_password():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp

    cur = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    new2 = request.form.get("new_password2") or ""

    errors = {}
    if not current_user.check_password(cur):
        errors["current_password"] = "Текущий пароль неверный"
    if len(new) < 8:
        errors["new_password"] = "Минимум 8 символов"
    if new != new2:
        errors["new_password2"] = "Пароли не совпадают"
    if new and current_user.check_password(new):
        errors["new_password"] = "Новый пароль должен отличаться от текущего"

    if errors:
        # Render the same page with errors. Status 422 is Turbo-friendly.
        return (
            render_template(
                "settings_security.html",
                current_user=current_user,
                errors=errors,
            ),
            422,
        )

    current_user.set_password(new)
    # Invalidate other sessions.
    current_user.session_version = int(current_user.session_version or 1) + 1
    db.session.add(current_user)
    db.session.commit()

    # Keep current session alive by updating snapshot version.
    session["session_version"] = int(current_user.session_version or 1)
    flash("Пароль изменён", "success")
    return redirect(url_for("settings_security"))


@app.route("/settings/security/logout_all", methods=["POST"])
def settings_logout_all():
    current_user, resp = _require_login_or_redirect()
    if resp:
        return resp

    current_user.session_version = int(current_user.session_version or 1) + 1
    db.session.add(current_user)
    db.session.commit()
    session["session_version"] = int(current_user.session_version or 1)
    flash("Ок: все другие сессии завершены", "success")
    return redirect(url_for("settings_security"))


@app.errorhandler(413)
def request_entity_too_large(e):
    """Единый обработчик для слишком больших загрузок."""
    flash(f"Файл слишком большой. Максимум {SUBMISSION_MAX_MB} МБ.", "error")
    # если загрузка шла через очередь — возвращаем туда
    try:
        if request.path.startswith("/queue"):
            return redirect(url_for("queue_page"))
    except Exception:
        pass
    return redirect(url_for("home"))



@app.route("/media/submissions/<string:file_uuid>.<string:ext>")
def submission_audio(file_uuid: str, ext: str):
    """Публичная раздача загруженных аудиофайлов (mp3/wav) с поддержкой Range."""
    if not _is_safe_uuid(file_uuid):
        return make_response("Not found", 404)

    ext = (ext or "").lower().lstrip(".")
    if ext not in ALLOWED_SUBMISSION_EXTS:
        return make_response("Not found", 404)
    # If S3 is configured, redirect to a short-lived presigned URL
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
            # fallback to local disk below

    filename = f"{file_uuid}.{ext}"
    file_path = os.path.join(SUBMISSIONS_RAW_DIR, filename)
    if not os.path.isfile(file_path):
        return make_response("Not found", 404)

    resp = send_from_directory(SUBMISSIONS_RAW_DIR, filename, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/queue", methods=["GET"])
def queue_page():
    """Публичная очередь треков + форма загрузки."""
    # активный трек для отображения (если есть)
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
    # Intentionally disabled: tracks must be submitted via Telegram bot.
    return jsonify({"error": "upload_disabled"}), 410


@app.route("/api/queue")
def api_queue_state():
    """JSON для очереди (можно использовать на фронте, в т.ч. для панели)."""
    payload = _serialize_queue_state(limit=200)
    payload["active"] = _get_playback_snapshot().get("active")
    return jsonify(payload)


# -------------------------
# Telegram Bot private API
# -------------------------

# Telegram bot private API token.
# Historical note: some deployments used TRACKRATER_TG_API_TOKEN (missing "RATER").
# Accept both to avoid silent 403s after migrations.
TG_API_TOKEN = (
    os.getenv("TRACKRATER_TG_API_TOKEN")
    or os.getenv("TRACKRATER_TG_API_TOKEN")
    or ""
).strip()

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

    # Local raw copy if needed (either no S3, or keep local is requested)
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
        # Optional local mirror for debugging / backup
        if S3_KEEP_LOCAL:
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            shutil.copyfile(tmp_path, raw_path)
    else:
        # Local storage (default)
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        shutil.copyfile(tmp_path, raw_path)

    # cleanup tmp
    try:
        os.remove(tmp_path)
    except Exception:
        pass

def _queue_position(submission_id: int) -> int:
    """Compute 1-based position in queue for queued/waiting items (excluding playing/done/deleted)."""
    # Stable sort: priority desc, priority_set_at asc, created_at asc, id asc
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

@app.route("/api/tg/submissions", methods=["POST"])
def tg_create_submission():
    """Create a draft submission from Telegram bot (stores file in tmp, not S3 yet)."""
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
    """Finalize file (tmp->S3/local) and put into queue with priority 0."""
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"error": "not found"}), 404
    if (sub.status or "") in ("deleted", "done"):
        return jsonify({"error": "invalid status"}), 400
    if not (sub.artist or "").strip() and not (sub.title or "").strip():
        return jsonify({"error": "missing metadata"}), 400

    # Finalize storage
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

    # IMPORTANT:
    # - Do NOT change queue priority until payment is confirmed (mark_paid)
    # - Do NOT remove already queued track from queue during "raise priority" flow
    sub.payment_status = "pending"
    sub.payment_amount = prio
    provider = (data.get("provider") or "").strip() or None
    ref = (data.get("provider_ref") or data.get("ref") or "").strip() or None
    if provider not in (None, "donationalerts"):
        return jsonify({"error": "bad provider"}), 400
    sub.payment_provider = provider
    sub.payment_ref = ref

    # For new submissions (not in queue yet) we keep an explicit waiting status,
    # but for queued/playing tracks we keep the current status.
    if (sub.status or "") not in ("queued", "playing"):
        sub.status = "waiting_payment"
    db.session.commit()
    _broadcast_queue_state()
    return jsonify({"ok": True})

@app.route("/api/tg/submissions/<int:submission_id>/mark_paid", methods=["POST"])
def tg_mark_paid(submission_id: int):
    """Mark payment as paid, finalize file to storage and put into queue with selected priority."""
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

    # idempotency: if already paid with same ref - ok
    if sub.payment_status == "paid":
        if sub.payment_ref == provider_ref:
            return jsonify({"ok": True, "position": _queue_position(submission_id)})
        return jsonify({"error": "already paid"}), 409

    # ensure amount matches chosen priority (or higher)
    required = int(sub.payment_amount or sub.priority or 0)
    if amount < required:
        return jsonify({"error": "amount too low", "required": required}), 400

    # Finalize storage only for new submissions that were not enqueued yet.
    if (sub.status or "") not in ("queued", "playing"):
        _finalize_tmp_to_storage(sub)

    # Apply priority only after confirmed payment (important for fair queue ordering)
    sub.priority = required

    # Keep "playing" as-is; otherwise put into queue
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
    """Best-effort cancel.

    Used by TG bot "Отмена" button to clean tails:
    - For draft/waiting_payment: mark deleted and remove tmp file
    - For queued/playing: clear pending payment fields (upgrade cancelled)
    """
    if not _require_tg_bot_token():
        return jsonify({"error": "forbidden"}), 403
    sub = db.session.get(TrackSubmission, submission_id)
    if not sub:
        return jsonify({"ok": True})

    status = (sub.status or "")
    if status in ("draft", "waiting_payment"):
        # delete tmp file if exists
        try:
            ext = (sub.original_ext or "").lower()
            tmp_path = _tmp_path_for(sub.file_uuid, ext)
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        sub.status = "deleted"

    # Clear payment tails in all cases
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

    # Only show tracks that are actually in queue right now.
    # (If track was not enqueued yet, or already removed/processed, it must not appear here.)
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




# ---------------- DonationAlerts OAuth (admin connect) ----------------

@app.route("/da/connect")
def da_connect():
    """Start DonationAlerts OAuth flow.

    Open this in browser while logged-in as site admin to connect DonationAlerts account.
    """
    # Optional admin-only guard: if auth system exists, we use it; otherwise allow.
    try:
        if callable(globals().get("current_user")) and hasattr(current_user, "role"):
            if getattr(current_user, "role", "") != "admin":
                return "Forbidden", 403
    except Exception:
        pass

    import secrets
    state = secrets.token_urlsafe(24)
    session["da_oauth_state"] = state
    scopes = "oauth-user-show oauth-donation-index"
    return redirect(build_authorize_url(state=state, scopes=scopes))


@app.route("/da/callback")
def da_callback():
    # Validate state (CSRF protection)
    state = request.args.get("state", "")
    expected = session.get("da_oauth_state", "")
    if expected and state != expected:
        return "Bad state", 400

    code = request.args.get("code", "")
    if not code:
        err = request.args.get("error") or "missing_code"
        return f"DonationAlerts auth failed: {err}", 400

    tokens = exchange_code_for_tokens(code)
    access = tokens.get("access_token")
    if not access:
        return "No access_token returned by DonationAlerts", 500

    user = fetch_user_oauth(access)
    # Persist for poller/listener usage
    store = load_tokens()
    store.update(tokens)
    # Try to persist user info we need later
    # user payload contains id, code(username), socket_connection_token etc.
    if isinstance(user, dict):
        for k in ("id", "code", "socket_connection_token", "main_currency"):
            if k in user:
                store[f"user_{k}"] = user.get(k)
    save_tokens(store)

    return (
        "<h2>DonationAlerts подключен ✅</h2>"
        "<p>Можно закрыть эту вкладку и вернуться в Telegram-бот.</p>"
    )


@app.route("/da/status")
def da_status():
    data = load_tokens()
    ok = bool(data.get("access_token") or data.get("refresh_token"))
    return jsonify({
        "connected": ok,
        "user_id": data.get("user_id"),
        "user": data.get("user_code"),
    })


@app.route("/")
def home():
    """
    Публичная главная страница ANTIGAZ Hub.
    Показывает ленту новостей, мини‑топ и последние оценённые треки.
    """
    # страница новостей (простая пагинация)
    page = request.args.get("page", 1, type=int)
    per_page = 10

    news_query = db.session.query(News).order_by(News.created_at.desc())
    news_pagination = news_query.paginate(page=page, per_page=per_page, error_out=False)

    # подготовим вложения для новостей на главной (поддержка нескольких файлов)
    page_news_ids = [n.id for n in news_pagination.items]
    attachments_by_news = {nid: [] for nid in page_news_ids}

    # новые вложения (через таблицу news_attachments)
    if page_news_ids:
        try:
            rows = (
                db.session.query(NewsAttachment)
                .filter(NewsAttachment.news_id.in_(page_news_ids))
                .order_by(NewsAttachment.uploaded_at.asc())
                .all()
            )
            for att in rows:
                attachments_by_news.setdefault(att.news_id, []).append(
                    {
                        "stored": att.stored_filename,
                        "original": att.original_filename,
                        "is_image": _is_image_filename(att.stored_filename),
                        "url": url_for("static", filename="uploads/news/" + att.stored_filename),
                    }
                )
        except Exception:
            # если таблицы ещё нет или что-то пошло не так — молча падаем на legacy-режим
            pass

    # legacy-вложение (старый формат: файл news_<id>_* в static/uploads)
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
                attachments_by_news[nid] = [
                    {
                        "stored": _fname,
                        "original": _fname.replace(_prefix, "", 1) or _fname,
                        "is_image": _is_image_filename(_fname),
                        "url": url_for("static", filename="uploads/" + _fname),
                    }
                ]
                break

    news_items = []
    for n in news_pagination.items:
        attachments = attachments_by_news.get(n.id, []) or []
        news_items.append(
            {
                "id": n.id,
                "title": n.title,
                "text": n.text,
                "tag": n.tag,
                "date": n.created_at.strftime("%d.%m.%Y") if n.created_at else None,
                "attachments": attachments,
            }
        )

    # Мини‑топ (топ‑3 по среднему баллу стримеров)
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
        .group_by(
            Track.id,
            Track.name,
            Track.created_at,
            viewer_subq.c.avg_viewers,
        )
    )

    top_rows = (
        base_query
        .order_by(func.avg(Evaluation.score).desc(), Track.created_at.desc())
        .limit(3)
        .all()
    )
    top_tracks = []
    for row in top_rows:
        top_tracks.append(
            {
                "id": row.track_id,
                "name": row.track_name,
                "avg_score": float(row.avg_streamers) if row.avg_streamers is not None else None,
            }
        )

    # Последние оценённые треки (3 штуки по дате добавления)
    recent_rows = (
        db.session.query(
            Track.id.label("track_id"),
            Track.name.label("track_name"),
            Track.created_at.label("created_at"),
            func.avg(Evaluation.score).label("avg_streamers"),
        )
        .join(Evaluation, Evaluation.track_id == Track.id)
        .filter(Track.is_deleted.is_(False))
        .group_by(
            Track.id,
            Track.name,
            Track.created_at,
        )
        .order_by(Track.created_at.desc())
        .limit(3)
        .all()
    )
    recent_tracks = []
    for row in recent_rows:
        recent_tracks.append(
            {
                "id": row.track_id,
                "name": row.track_name,
                "created_at": row.created_at,
                "final_score": float(row.avg_streamers) if row.avg_streamers is not None else None,
            }
        )


    # Комментарии к трекам (для модерации)
    comments_rows = (
        db.session.query(TrackComment, Track)
        .join(Track, Track.id == TrackComment.track_id)
        .filter(TrackComment.is_deleted.is_(False))
        .order_by(TrackComment.is_approved.asc(), TrackComment.created_at.desc())
        .limit(100)
        .all()
    )

    # Конфиг стрима
    cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
    stream_info = None
    if cfg and cfg.is_active and cfg.url:
        stream_info = {
            "title": cfg.title or "Стрим на Twitch",
            "url": cfg.url,
        }

    return render_template(
        "home.html",
        news_items=news_items,
        news_pagination=news_pagination,
        top_tracks=top_tracks,
        recent_tracks=recent_tracks,
        stream_info=stream_info,
    )


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

@app.route("/admin/news/<int:news_id>/delete", methods=["POST"])
def delete_news(news_id):
    if not _require_admin():
        return redirect(url_for("login"))

    news = db.session.get(News, news_id)
    if not news:
        flash("Новость не найдена", "error")
        return redirect(url_for("admin"))

    # удалить файлы вложений (новый формат)
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

    # удалить legacy-вложение (старый формат: news_<id>_* в static/uploads)
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

        # множественные вложения
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

        # добавить новые вложения (если есть)
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

    # подготовим вложения (новый формат + legacy)
    attachments = []
    try:
        for att in getattr(news, "attachments", []) or []:
            attachments.append(
                {
                    "id": att.id,
                    "stored": att.stored_filename,
                    "original": att.original_filename,
                    "is_image": _is_image_filename(att.stored_filename),
                    "url": url_for("static", filename="uploads/news/" + att.stored_filename),
                }
            )
    except Exception:
        pass

    # legacy (если есть и нет новых)
    if not attachments:
        try:
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith(f"news_{news.id}_"):
                    attachments.append(
                        {
                            "id": None,
                            "stored": fname,
                            "original": fname.replace(f"news_{news.id}_", "", 1) or fname,
                            "is_image": _is_image_filename(fname),
                            "url": url_for("static", filename="uploads/" + fname),
                        }
                    )
            # legacy может быть несколько, но оставим все найденные
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
@app.route("/admin", methods=["GET", "POST"])
def admin():
    """
    Админка ANTIGAZ.
    Сейчас: управление новостями и блоком стрима.
    """
    if not _require_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        form_type = request.form.get("form") or "news"

        # Управление пользователями (только для супер-админа)
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

                # нельзя удалить супер-админа и самого себя
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

                # Нельзя разжаловать самого себя (чтобы не потерять доступ к админке)
                if current and current.id == user.id and new_role != user.role:
                    flash("Нельзя менять роль самому себе", "error")
                    return redirect(url_for("admin", tab="users"))

                # Нельзя менять роль главного админа через UI
                if user.is_superadmin() and new_role != "superadmin":
                    flash("Нельзя менять роль главного админа", "error")
                    return redirect(url_for("admin", tab="users"))

                user.role = new_role
                db.session.commit()
                flash("Роль обновлена", "success")
                return redirect(url_for("admin", tab="users"))


        # Модерация комментариев к трекам
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

        # Добавление новости
        if form_type == "news":
            title = (request.form.get("title") or "").strip()
            tag = (request.form.get("tag") or "").strip() or None

            # поддержка старого поля text и нового text_html из редактора
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

                # множественные вложения (новый формат)
                files = request.files.getlist("attachments")
                if not files:
                    # legacy: одно поле attachment
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
 

        # Обновление настроек стрима (тоггл: начать / закончить)
        elif form_type == "stream":
            print("STREAM FORM DATA:", dict(request.form))

            cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
            if not cfg:
                cfg = StreamConfig()
                db.session.add(cfg)

            # если стрим уже активен — по нажатию просто останавливаем и очищаем данные
            if cfg.is_active:
                cfg.is_active = False
                cfg.title = ""
                cfg.url = ""
                db.session.commit()
                flash("Стрим завершён", "success")
                return redirect(url_for("admin", tab="stream"))

            # если стрим не активен — пробуем запустить
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


        # Генерация / обновление ссылки на OBS-виджет
        elif form_type == "stream_widget":
            cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
            if not cfg:
                cfg = StreamConfig()
                db.session.add(cfg)

            cfg.widget_token = uuid4().hex
            db.session.commit()
            flash("Ссылка для OBS-виджета обновлена", "success")
            return redirect(url_for("admin", tab="stream"))

    # Комментарии к трекам (для модерации)
    comments_rows = (
        db.session.query(TrackComment, Track)
        .join(Track, Track.id == TrackComment.track_id)
        .filter(TrackComment.is_deleted.is_(False))
        .order_by(TrackComment.is_approved.asc(), TrackComment.created_at.desc())
        .limit(100)
        .all()
    )

    # пагинация новостей в админке
    page = request.args.get("page", 1, type=int)
    per_page = 5

    news_query = db.session.query(News).order_by(News.created_at.desc())
    news_pagination = news_query.paginate(page=page, per_page=per_page, error_out=False)
    news_list = news_pagination.items

    # вложения для новостей в админке (поддержка нескольких файлов)
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
                attachments.setdefault(att.news_id, []).append(
                    {
                        "id": att.id,
                        "stored": att.stored_filename,
                        "original": att.original_filename,
                        "is_image": _is_image_filename(att.stored_filename),
                        "url": url_for("static", filename="uploads/news/" + att.stored_filename),
                    }
                )
        except Exception:
            pass

    # legacy-вложения (старый формат)
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
                attachments[n.id] = [
                    {
                        "id": None,
                        "stored": fname,
                        "original": fname.replace(prefix, "", 1) or fname,
                        "is_image": _is_image_filename(fname),
                        "url": url_for("static", filename="uploads/" + fname),
                    }
                ]
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




# -------------------------
# Admin actions
# -------------------------
@app.route("/admin/clear_queue", methods=["POST"])
@app.route("/admin/clear_queue", methods=["POST"])
def admin_clear_queue():
    # Проверка прав — В ТВОЁМ стиле
    if not _require_admin():
        return redirect(url_for("login"))

    # Обязательное подтверждение
    if (request.form.get("confirm") or "").strip() != "yes":
        flash("Очистка очереди отменена.", "warning")
        return redirect(url_for("index"))

    # Мягкая очистка: только queued → deleted
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

    # ВАЖНО: сразу обновляем всем очередь
    try:
        _broadcast_queue_state()
    except Exception:
        pass

    return redirect(url_for("index"))



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
    """
    OBS-виджет, который подключается как Browser Source в OBS.
    Доступен только по корректному токену stream_config.widget_token.
    """
    cfg = db.session.query(StreamConfig).order_by(StreamConfig.id.asc()).first()
    if not cfg or not cfg.widget_token or cfg.widget_token != token:
        return make_response("Widget is not configured", 404)

    return render_template("obs_widget.html")

@app.route("/track/<int:track_id>", methods=["GET"])
def track_page(track_id: int):
    """Public track page (server-rendered).

    Shows streamer ratings summary + user reviews.
    Product decision: reviews replace comments; one user can leave one review per track.
    """
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        flash("Трек не найден", "error")
        return redirect(url_for("top_tracks"))

    user = get_current_user()
    is_admin = bool(user and user.is_admin())

    # Own review (if any)
    my_review = None
    if user:
        my_review = (
            db.session.query(TrackReview)
            .filter(TrackReview.track_id == track.id, TrackReview.user_id == user.id)
            .first()
        )

    # Prefill per-criterion scores for the review form
    my_review_score_map = {key: 0 for key, _label in CRITERIA}
    if my_review and getattr(my_review, "scores", None):
        try:
            for s in my_review.scores:
                if s.criterion_key in my_review_score_map:
                    my_review_score_map[s.criterion_key] = int(s.score)
        except Exception:
            pass

    # --- Streamers ---
    overall_avg = (
        db.session.query(func.avg(Evaluation.score))
        .filter(Evaluation.track_id == track.id)
        .scalar()
    )
    overall_avg = float(overall_avg) if overall_avg is not None else None

    crit_rows = (
        db.session.query(
            Evaluation.criterion_key,
            func.avg(Evaluation.score).label("avg_score"),
        )
        .filter(Evaluation.track_id == track.id)
        .group_by(Evaluation.criterion_key)
        .order_by(Evaluation.criterion_key)
        .all()
    )
    criteria_stats = [{"key": row.criterion_key, "avg": float(row.avg_score)} for row in crit_rows]

    rater_rows = (
        db.session.query(
            Evaluation.rater_name,
            func.avg(Evaluation.score).label("avg_score"),
        )
        .filter(Evaluation.track_id == track.id)
        .group_by(Evaluation.rater_name)
        .order_by(Evaluation.rater_name)
        .all()
    )
    raters_stats = [{"name": row.rater_name, "avg": float(row.avg_score)} for row in rater_rows]

    # --- User reviews ---
    review_overall_val = (
        db.session.query(func.avg(TrackReview.overall))
        .filter(TrackReview.track_id == track.id)
        .scalar()
    )
    review_overall = float(review_overall_val) if review_overall_val is not None else None
    review_count = (
        db.session.query(func.count(TrackReview.id))
        .filter(TrackReview.track_id == track.id)
        .scalar()
        or 0
    )
    reviews = (
        db.session.query(TrackReview)
        .filter(TrackReview.track_id == track.id)
        .order_by(TrackReview.created_at.desc())
        .all()
    )

    # If track is linked to a viewer submission, show embedded audio
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

    # Scores per criterion (0..10)
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
        # Upsert scores
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
        db.session.flush()  # assign review.id before inserting scores
        for k, v in scores.items():
            db.session.add(TrackReviewScore(review_id=review.id, criterion_key=k, score=v))
        flash("Рецензия опубликована", "success")

    db.session.commit()
    return redirect(url_for("track_page", track_id=track.id) + "#reviews")


@app.route("/top")
def top_tracks():
    """Top tracks.

    `sort_by=viewers` is kept for backward compatibility, but it now means
    "users" i.e. average overall score from reviews (TrackReview.overall).
    """
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
        .group_by(
            Track.id,
            Track.name,
            Track.created_at,
            review_subq.c.avg_viewers,
        )
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
        tracks.append(
            {
                "position": offset + idx_row + 1,
                "id": row.track_id,
                "name": row.track_name,
                "created_at": row.created_at,
                "avg_streamers": float(row.avg_streamers) if row.avg_streamers is not None else None,
                "avg_viewers": float(row.avg_viewers) if row.avg_viewers is not None else None,
            }
        )

    return render_template(
        "top.html",
        tracks=tracks,
        page=page,
        total_pages=total_pages,
        sort_by=sort_by,
        direction=direction,
        is_admin=_require_admin(),
    )



@app.route("/admin/tracks/<int:track_id>/rename", methods=["POST"])
def admin_rename_track(track_id: int):
    if not _require_admin():
        return jsonify({"error": "forbidden"}), 403

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
        return jsonify({"error": "forbidden"}), 403

    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    track.is_deleted = True
    db.session.commit()
    return jsonify({"success": True})




@app.route("/viewers")
def viewers_page():
    """
    Публичная страница для зрителей.
    Список треков по дате добавления (новые сверху) + пагинация.
    При первом заходе выдаём viewer_id в cookie.
    """
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


@app.route("/api/viewers/track/<int:track_id>")
def viewer_track_summary(track_id: int):
    """
    JSON для модалки зрителя:
    - информация о треке
    - уже ли голосовал этот viewer
    - средние оценки зрителей по критериям
    - общая средняя зрителей
    """
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    viewer_id = request.cookies.get(VIEWER_COOKIE_NAME)

    has_voted = False
    viewer_scores = {}
    if viewer_id:
        rows = (
            ViewerRating.query
            .filter_by(viewer_id=viewer_id, track_id=track_id)
            .all()
        )
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
        criteria_stats.append(
            {
                "key": key,
                "label": label,
                "avg_score": avg_val,
            }
        )

    overall_avg = (
        db.session.query(func.avg(ViewerRating.score))
        .filter(ViewerRating.track_id == track_id)
        .scalar()
        or 0.0
    )

    return jsonify(
        {
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
        }
    )


@app.route("/api/viewers/rate", methods=["POST"])
def viewers_rate():
    """
    Принять оценки от зрителя:
    body: { "track_id": int, "ratings": {criterion_key: score(int 0-10)} }
    Ограничение: 1 зритель (viewer_id) → 1 трек.
    """
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

    existing_count = (
        ViewerRating.query
        .filter_by(viewer_id=viewer_id, track_id=track_id)
        .count()
    )
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

    return jsonify(
        {
            "status": "ok",
            "overall_avg": float(overall_avg),
        }
    )
@app.route("/api/track/<int:track_id>/summary")
def track_summary(track_id: int):
    """
    JSON-сводка по треку:
    - общая средняя стримеров
    - средняя по критериям (стримеры)
    - средняя по оценщикам (стримеры)
    - средняя оценка пользователей по рецензиям
    - количество рецензий
    """
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        return jsonify({"error": "not_found"}), 404

    # --- стримеры ---
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
        criteria.append(
            {"key": key, "label": label, "avg": avg_val}
        )

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
    raters = [
        {"name": row.rater_name, "avg": float(row.avg_score)}
        for row in rater_rows
    ]

    # --- users (reviews) ---
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
    # Keep "viewer_criteria" field for backward compatibility with the frontend modal.
    viewer_criteria = []

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
        "viewer_criteria": viewer_criteria,
        "review_count": int(review_count),
    }
    return jsonify(payload)

