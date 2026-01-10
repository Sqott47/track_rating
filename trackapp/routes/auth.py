"""Authentication routes: login, register, logout, password reset, settings.

Migrated from the monolithic routes.py for better maintainability.
"""

from datetime import datetime, timedelta
import re

from flask import request, redirect, url_for, session, flash, render_template

from ..core import app, db, get_current_user, ADMIN_USERNAME, ADMIN_PASSWORD
from ..core import _init_default_raters
from ..models import User, EmailVerificationToken, PasswordResetToken
from ..mailer import generate_token, sha256_hex, resend_send_email


# -----------------
# Login / Register / Logout
# -----------------

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
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Open registration (outside Turbo)."""
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

        if not username:
            errors["username"] = "Логин обязателен"
        elif not re.fullmatch(r"[a-zA-Z0-9_]{3,20}", username):
            errors["username"] = "Логин: 3–20 символов, только латиница/цифры/_"
        else:
            if db.session.query(User).filter_by(username=username).first():
                errors["username"] = "Этот логин уже занят"

        if not email:
            errors["email"] = "Email обязателен"
        elif "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Неверный формат email"
        else:
            if db.session.query(User).filter_by(email=email).first():
                errors["email"] = "Этот email уже используется"

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

            session["user"] = user.username
            session["role"] = user.role
            session["session_version"] = int(user.session_version or 1)

            _send_verification_email(user)
            flash("Аккаунт создан! Мы отправили письмо для подтверждения email.", "success")
            return redirect(url_for("top_tracks"))

        flash("Исправьте ошибки в форме", "error")
        return render_template("register.html", errors=errors, form=form), 422

    return render_template("register.html", errors=errors, form=form)


@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("role", None)
    session.pop("session_version", None)
    return redirect(url_for("login"))


# -----------------
# Password Reset
# -----------------

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
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

    changed = (current_user.email or "").lower() != email
    current_user.email = email
    if changed:
        current_user.email_verified_at = None
    db.session.add(current_user)
    db.session.commit()

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

    if user.email_verified_at is not None:
        row.used_at = datetime.utcnow()
        db.session.add(row)
        db.session.commit()
        flash("Email уже подтверждён", "info")
        return redirect(url_for("settings_profile")) if session.get("user") else redirect(url_for("login"))

    now = datetime.utcnow()
    row.used_at = now
    user.email_verified_at = now

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
    if session.get("user"):
        return redirect(url_for("settings_profile"))
    return redirect(url_for("login"))


def _send_verification_email(user: User) -> None:
    if user.email_verified_at is not None:
        return

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
        app.logger.warning("Could not send verification email: %s", msg)


# -----------------
# Security Settings
# -----------------

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
        return (
            render_template(
                "settings_security.html",
                current_user=current_user,
                errors=errors,
            ),
            422,
        )

    current_user.set_password(new)
    current_user.session_version = int(current_user.session_version or 1) + 1
    db.session.add(current_user)
    db.session.commit()

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
