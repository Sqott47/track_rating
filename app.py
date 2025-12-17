import os
import threading
from datetime import datetime
from uuid import uuid4
import io
import qrcode
import subprocess
import shutil
from typing import Optional, Dict, Any, List

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    jsonify,
    make_response,
    send_from_directory,
)
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy import func, text


BASE_DIR = os.path.abspath(os.path.dirname(__file__))

UPLOAD_DIR = os.getenv(
    "UPLOAD_DIR",
    os.path.join(BASE_DIR, "static", "uploads"),
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# -------------------------
# Track submissions (queue)
# -------------------------
# Храним исходники и сконверченные mp3 в подпапках UPLOAD_DIR.
# В проде UPLOAD_DIR обычно указывает на shared/, поэтому всё окажется в shared.
SUBMISSIONS_RAW_DIR = os.path.join(UPLOAD_DIR, "submissions_raw")
os.makedirs(SUBMISSIONS_RAW_DIR, exist_ok=True)
# SUBMISSIONS_MP3_DIR ранее использовался для перекодирования.
# Сейчас конвертацию отключаем полностью: храним исходники как есть (mp3/wav)

SUBMISSION_MAX_MB = int(os.getenv("SUBMISSION_MAX_MB", "50"))
ALLOWED_SUBMISSION_EXTS = {"mp3", "wav"}
# -------------------------
# Optional S3 storage (Timeweb S3 / S3-compatible)
# Works on server with env vars, and does nothing locally on Windows by default.
# -------------------------
S3_ENABLED = os.getenv("S3_ENABLED", "0") == "1"
S3_ENDPOINT_URL = (os.getenv("S3_ENDPOINT_URL") or "").strip()
S3_BUCKET = (os.getenv("S3_BUCKET") or "").strip()
S3_REGION = (os.getenv("S3_REGION") or "").strip()
S3_PREFIX = (os.getenv("S3_PREFIX") or "submissions_raw/").strip()
S3_PRESIGN_EXPIRES = int(os.getenv("S3_PRESIGN_EXPIRES", "1800"))
S3_KEEP_LOCAL = os.getenv("S3_KEEP_LOCAL", "0") == "1"  # optional fallback

_s3_client = None

def _s3_is_configured() -> bool:
    return bool(S3_ENABLED and S3_BUCKET and S3_ENDPOINT_URL)

def _s3_key_for_submission(file_uuid: str, ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    prefix = S3_PREFIX or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{file_uuid}.{ext}"

def _get_s3_client():
    """
    Lazy import boto3 so local dev works without boto3 installed.
    """
    global _s3_client
    if not _s3_is_configured():
        return None
    if _s3_client is not None:
        return _s3_client

    try:
        import boto3
        from botocore.config import Config
    except Exception as e:
        print("S3 enabled but boto3/botocore not available:", e)
        return None

    cfg = Config(signature_version="s3v4")
    _s3_client = boto3.client(
        "s3",
        region_name=S3_REGION or None,
        endpoint_url=S3_ENDPOINT_URL,
        config=cfg,
    )
    return _s3_client


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change_this_secret_key")
# Limit upload size (50MB by default). Nginx must also allow this via client_max_body_size.
app.config["MAX_CONTENT_LENGTH"] = SUBMISSION_MAX_MB * 1024 * 1024
DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(BASE_DIR, "track_ratings.db"),
)

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# SQLite по умолчанию ограничивает доступ к соединению из одного потока.
# Мы используем фоновые задачи (конвертация аудио), поэтому явно разрешаем
# межпоточный доступ для sqlite.
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
    app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {})
    engine_opts = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    engine_opts.setdefault("connect_args", {})
    engine_opts["connect_args"].setdefault("check_same_thread", False)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# (key, label)
CRITERIA = [
    ("rhyme", "Текст + Рифмы"),
    ("structure", "Структура + Ритмика"),
    ("style", "Реализация стиля + Жанра"),
    ("quality", "Качество + Сведение"),
    ("vibe", "Вайб + Общее впечатление"),
]

DEFAULT_NUM_RATERS = 4

VIEWER_COOKIE_NAME = "antigaz_viewer_id"
VIEWER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

def _get_or_create_viewer_id():
    vid = request.cookies.get(VIEWER_COOKIE_NAME)
    if vid:
        return vid
    # lazy import to avoid circular issues
    from uuid import uuid4
    return uuid4().hex





class News(db.Model):
    __tablename__ = "news_items"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    text = db.Column(db.Text, nullable=True)
    tag = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class StreamConfig(db.Model):
    __tablename__ = "stream_config"

    id = db.Column(db.Integer, primary_key=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    title = db.Column(db.String(255), nullable=True)
    url = db.Column(db.String(512), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    widget_token = db.Column(db.String(64), nullable=True)


class TrackSubmission(db.Model):
    """Публичная очередь треков от зрителей.

    Зритель загружает файл (wav/mp3), на сервере конвертируем в mp3.
    Админ выставляет приоритет (0/100/200/300/400) и выбирает активный трек.
    """

    __tablename__ = "track_submissions"

    id = db.Column(db.Integer, primary_key=True)
    artist = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    priority = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(
        db.String(32),
        nullable=False,
        default="converting",  # converting|queued|playing|done|deleted|failed
    )
    file_uuid = db.Column(db.String(32), nullable=False, unique=True, index=True)
    original_filename = db.Column(db.String(255), nullable=True)
    original_ext = db.Column(db.String(16), nullable=False)
    duration_sec = db.Column(db.Integer, nullable=True)
    linked_track_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Время, когда текущий priority был установлен. Нужен как стабильный tie-breaker
    # для FIFO внутри одного уровня приоритета (чтобы более ранний 200 не мог быть
    # перебит более поздним 200).
    priority_set_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Track(db.Model):
    __tablename__ = "tracks"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    # Если трек пришёл из очереди (загрузка зрителем) — ссылка на submission.
    submission_id = db.Column(db.Integer, db.ForeignKey("track_submissions.id"), nullable=True)


class Evaluation(db.Model):
    __tablename__ = "evaluations"
    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), nullable=False)
    rater_name = db.Column(db.String(255), nullable=False)
    criterion_key = db.Column(db.String(50), nullable=False)
    score = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ViewerRating(db.Model):
    __tablename__ = "viewer_ratings"

    id = db.Column(db.Integer, primary_key=True)
    viewer_id = db.Column(db.String(64), index=True, nullable=False)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), index=True, nullable=False)
    criterion_key = db.Column(db.String(50), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)




class TrackComment(db.Model):
    __tablename__ = "track_comments"

    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), index=True, nullable=False)
    author_name = db.Column(db.String(64), nullable=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_approved = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)





class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="admin")  # "superadmin" или "admin"
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")


    def is_judge(self) -> bool:
        return self.role in ("judge", "admin", "superadmin")



with app.app_context():
    db.create_all()
    # Ensure soft-delete column exists on tracks
    try:
        rows = db.session.execute(text("PRAGMA table_info(tracks)")).fetchall()
        cols = [r[1] for r in rows]
        if "is_deleted" not in cols:
            db.session.execute(text("ALTER TABLE tracks ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"))
            db.session.commit()
    except Exception as e:
        # Do not crash app if migration fails; just log
        print("Warning: could not ensure is_deleted column:", e)

    # Ensure submission_id exists on tracks (link to queue submissions)
    try:
        rows = db.session.execute(text("PRAGMA table_info(tracks)")).fetchall()
        cols = [r[1] for r in rows]
        if "submission_id" not in cols:
            db.session.execute(text("ALTER TABLE tracks ADD COLUMN submission_id INTEGER"))
            db.session.commit()
    except Exception as e:
        print("Warning: could not ensure submission_id column:", e)

    # Ensure widget_token exists on stream_config
    try:
        rows = db.session.execute(text("PRAGMA table_info(stream_config)")).fetchall()
        cols = [r[1] for r in rows]
        if "widget_token" not in cols:
            db.session.execute(
                text("ALTER TABLE stream_config ADD COLUMN widget_token TEXT")
            )
            db.session.commit()
    except Exception as e:
        print("Warning: could not ensure widget_token column:", e)

    # Ensure priority_set_at exists on track_submissions (stable FIFO within same priority)
    try:
        rows = db.session.execute(text("PRAGMA table_info(track_submissions)")).fetchall()
        cols = [r[1] for r in rows]
        if "priority_set_at" not in cols:
            # SQLite will store DateTime as TEXT; we keep it nullable during migration and then backfill.
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN priority_set_at DATETIME"))
            db.session.execute(text("UPDATE track_submissions SET priority_set_at = created_at WHERE priority_set_at IS NULL"))
            db.session.commit()
    except Exception as e:
        print("Warning: could not ensure priority_set_at column:", e)


    # инициализация супер-админа, если пользователей ещё нет
    try:
        if not db.session.query(User).count():
            sa = User(
                username=ADMIN_USERNAME,
                password=ADMIN_PASSWORD,
                role="superadmin",
            )
            db.session.add(sa)
            db.session.commit()
            print(f"Created default superadmin user: {ADMIN_USERNAME}")
    except Exception as e:
        print("Warning: could not ensure default superadmin:", e)

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



def get_current_user():
    username = session.get("user")
    if not username:
        return None
    return db.session.query(User).filter_by(username=username).first()


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
    global _next_rater_id
    with state_lock:
        if shared_state["raters"]:
            return
        for i in range(DEFAULT_NUM_RATERS):
            rid = str(_next_rater_id)
            _next_rater_id += 1
            shared_state["raters"][rid] = {
                "id": rid,
                "name": f"Оценщик {i + 1}",
                "order": i,
                "scores": {key: 0 for key, _ in CRITERIA},
            }


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
        socketio.emit("queue_state", payload)
    except Exception as e:
        print("Warning: failed to broadcast queue_state:", e)


def _broadcast_playback_state() -> None:
    try:
        payload = _get_playback_snapshot()
        socketio.emit("playback_state", payload)
    except Exception as e:
        print("Warning: failed to broadcast playback_state:", e)


def _convert_submission_worker(submission_id: int) -> None:
    """Конвертация временно отключена.

    Исторически очередь поддерживала перекодирование wav -> mp3, но это
    создавало блокировки/залипания в реальном времени. Сейчас храним
    исходники как есть (mp3/wav) и ничего не конвертируем.
    """
    return


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # сначала пробуем найти пользователя в БД
        user = None
        if username:
            user = db.session.query(User).filter_by(username=username).first()

        if user and user.password == password:
            session["user"] = user.username
            session["role"] = user.role
            _init_default_raters()
            return redirect(url_for("index"))

        # fallback на старый режим единственного админа из ENV
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["user"] = ADMIN_USERNAME
            session["role"] = "superadmin"
            _init_default_raters()
            return redirect(url_for("index"))

        flash("Неверный логин или пароль", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("role", None)
    return redirect(url_for("login"))


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


def _is_safe_uuid(value: str) -> bool:
    if not value:
        return False
    if len(value) > 64:
        return False
    # uuid4().hex — только [0-9a-f]
    for ch in value:
        if ch not in "0123456789abcdef":
            return False
    return True


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
    """Принять трек в очередь (публично)."""
    artist = (request.form.get("artist") or "").strip()
    title = (request.form.get("title") or "").strip()
    file = request.files.get("file")

    if not artist and not title:
        flash("Укажи хотя бы исполнителя или название трека.", "error")
        return redirect(url_for("queue_page"))

    if not file or not file.filename:
        flash("Прикрепи аудиофайл (.mp3 или .wav).", "error")
        return redirect(url_for("queue_page"))

    # Важно: secure_filename() может "съесть" кириллицу полностью.
    # Расширение берём из исходного имени, а на диск сохраняем под uuid.
    incoming_name = file.filename
    dot_ext = os.path.splitext(incoming_name)[1].lower()
    ext = dot_ext.lstrip(".")
    if ext not in ALLOWED_SUBMISSION_EXTS:
        flash("Неподдерживаемый формат. Разрешены: " + ", ".join(sorted(ALLOWED_SUBMISSION_EXTS)), "error")
        return redirect(url_for("queue_page"))

    file_uuid = uuid4().hex
    raw_filename = f"{file_uuid}.{ext}"
    raw_path = os.path.join(SUBMISSIONS_RAW_DIR, raw_filename)

    # Конвертацию отключаем: сразу считаем трек готовым.
    sub = TrackSubmission(
        artist=artist or "",
        title=title or "",
        priority=0,
        status="queued",
        file_uuid=file_uuid,
        original_filename=incoming_name,
        original_ext=ext,
        created_at=datetime.utcnow(),
        priority_set_at=datetime.utcnow(),
    )
    db.session.add(sub)
    db.session.commit()

    try:
        # 1) S3 (if configured)
        s3 = _get_s3_client()
        if s3 and _s3_is_configured():
            key = _s3_key_for_submission(file_uuid, ext)

            # rewind stream (important)
            try:
                file.stream.seek(0)
            except Exception:
                pass

            content_type = "audio/mpeg" if ext == "mp3" else "audio/wav"
            s3.upload_fileobj(
                Fileobj=file.stream,
                Bucket=S3_BUCKET,
                Key=key,
                ExtraArgs={"ContentType": content_type},
            )

            # 2) Optional local fallback copy
            if S3_KEEP_LOCAL:
                try:
                    file.stream.seek(0)
                except Exception:
                    pass
                file.save(raw_path)

        else:
            # Local dev / no S3
            file.save(raw_path)

    except Exception as e:
        print("Failed to save submission file:", e)
        sub.status = "failed"
        db.session.commit()
        flash("Не удалось сохранить файл. Попробуй ещё раз.", "error")
        return redirect(url_for("queue_page"))

    _broadcast_queue_state()
    flash("Трек добавлен в очередь.", "success")
    return redirect(url_for("queue_page"))


@app.route("/api/queue")
def api_queue_state():
    """JSON для очереди (можно использовать на фронте, в т.ч. для панели)."""
    payload = _serialize_queue_state(limit=200)
    payload["active"] = _get_playback_snapshot().get("active")
    return jsonify(payload)





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

    # подготовим карту вложений для новостей на главной
    try:
        _home_filenames = os.listdir(UPLOAD_DIR)
    except FileNotFoundError:
        _home_filenames = []

    _home_attachments = {}
    for _n in news_pagination.items:
        _prefix = f"news_{_n.id}_"
        for _fname in _home_filenames:
            if _fname.startswith(_prefix):
                _home_attachments[_n.id] = _fname
                break

    news_items = []
    for n in news_pagination.items:
        attachment = _home_attachments.get(n.id)
        is_image = False
        if attachment:
            _lname = attachment.lower()
            for _ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
                if _lname.endswith(_ext):
                    is_image = True
                    break

        news_items.append(
            {
                "id": n.id,
                "title": n.title,
                "text": n.text,
                "tag": n.tag,
                "date": n.created_at.strftime("%d.%m.%Y") if n.created_at else None,
                "attachment": attachment,
                "attachment_is_image": is_image,
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

    db.session.delete(news)
    db.session.commit()
    flash("Новость удалена", "success")
    return redirect(url_for("admin"))

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
            text = (request.form.get("text") or "").strip()
            tag = (request.form.get("tag") or "").strip() or None

            if not title:
                flash("Заголовок не может быть пустым", "error")
            else:
                news = News(title=title, text=text or None, tag=tag)
                db.session.add(news)
                db.session.commit()

                # обработка вложенного файла
                file = request.files.get("attachment")
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    if filename:
                        stored_name = f"news_{news.id}_" + filename
                        file_path = os.path.join(UPLOAD_DIR, stored_name)
                        try:
                            file.save(file_path)
                        except Exception as e:
                            print("Failed to save attachment for news", news.id, e)
                            flash("Новость добавлена, но вложение сохранить не удалось", "warning")
                        else:
                            flash("Новость добавлена", "success")
                            return redirect(url_for("admin"))

                flash("Новость добавлена", "success")
                return redirect(url_for("admin"))

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

    # карта вложений: news_id -> имя файла (если есть)
    attachments = {}
    try:
        filenames = os.listdir(UPLOAD_DIR)
    except FileNotFoundError:
        filenames = []

    for n in news_list:
        prefix = f"news_{n.id}_"
        for fname in filenames:
            if fname.startswith(prefix):
                attachments[n.id] = fname
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




def _get_track_url(track_id: int) -> str:
    """
    Каноническая ссылка на публичную страницу трека.
    """
    return url_for("track_page", track_id=track_id, _external=True)




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

@app.route("/track/<int:track_id>", methods=["GET", "POST"])
def track_page(track_id: int):
    """
    Публичная страница трека:
    - краткая информация
    - сводка оценок стримеров и зрителей
    - список оценщиков
    - комментарии (с модерацией в админке)
    """
    track = db.session.get(Track, track_id)
    if (not track) or getattr(track, "is_deleted", False):
        flash("Трек не найден", "error")
        return redirect(url_for("top_tracks"))


    if request.method == "POST":
        user = get_current_user()
        # удаление комментария (для админа)
        delete_id = request.form.get("delete_comment_id", type=int)
        if delete_id:
            if not user or not user.is_admin():
                flash("Недостаточно прав для удаления комментария", "error")
            else:
                comment_obj = db.session.get(TrackComment, delete_id)
                if comment_obj and not comment_obj.is_deleted:
                    comment_obj.is_deleted = True
                    db.session.commit()
                    flash("Комментарий скрыт", "success")
                else:
                    flash("Комментарий не найден", "error")
            return redirect(url_for("track_page", track_id=track.id) + "#comments")

        # добавление комментария
        author = (request.form.get("author") or "").strip()
        text = (request.form.get("text") or "").strip()
        if not text:
            flash("Комментарий не может быть пустым", "error")
        else:
            if len(author) > 64:
                author = author[:64]
            comment = TrackComment(
                track_id=track.id,
                author_name=author or None,
                text=text,
                is_approved=True,
            )
            db.session.add(comment)
            db.session.commit()
            flash("Комментарий добавлен", "success")
        return redirect(url_for("track_page", track_id=track.id) + "#comments")

    # --- стримеры: средний балл по треку ---
    overall_avg = (
        db.session.query(func.avg(Evaluation.score))
        .filter(Evaluation.track_id == track.id)
        .scalar()
    )
    overall_avg = float(overall_avg) if overall_avg is not None else None

    # средний по критериям (стримеры)
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
    criteria_stats = [
        {"key": row.criterion_key, "avg": float(row.avg_score)}
        for row in crit_rows
    ]

    # средний по оценщикам (стримеры)
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
    raters_stats = [
        {"name": row.rater_name, "avg": float(row.avg_score)}
        for row in rater_rows
    ]

    # --- зрители ---
    viewer_overall_val = (
        db.session.query(func.avg(ViewerRating.score))
        .filter(ViewerRating.track_id == track.id)
        .scalar()
    )
    viewer_overall = float(viewer_overall_val) if viewer_overall_val is not None else None

    viewer_crit_rows = (
        db.session.query(
            ViewerRating.criterion_key,
            func.avg(ViewerRating.score).label("avg_score"),
        )
        .filter(ViewerRating.track_id == track.id)
        .group_by(ViewerRating.criterion_key)
        .order_by(ViewerRating.criterion_key)
        .all()
    )
    viewer_criteria_stats = [
        {"key": row.criterion_key, "avg": float(row.avg_score)}
        for row in viewer_crit_rows
    ]

    # комментарии по треку (показываем все, кроме скрытых)
    comments = (
        db.session.query(TrackComment)
        .filter(
            TrackComment.track_id == track.id,
            TrackComment.is_deleted.is_(False),
        )
        .order_by(TrackComment.created_at.desc())
        .all()
    )


    user = get_current_user()
    is_admin = bool(user and user.is_admin())

    # если трек связан с файлом из очереди — покажем плеер
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

    
    # Player meta for embedded (track page) audio player
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
            viewer_overall=viewer_overall,
            viewer_criteria_stats=viewer_criteria_stats,
            comments=comments,
            CRITERIA=CRITERIA,
            is_admin=is_admin,
        )


@app.route("/top")
def top_tracks():
    """
    Страница с топом треков.
    Можно сортировать по среднему баллу стримеров или зрителей (asc/desc).
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

    if sort_by == "viewers":
        sort_col = viewer_subq.c.avg_viewers
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
    - средняя по критериям (зрители)
    - общая средняя зрителей
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

    # --- зрители ---
    viewer_overall_val = (
        db.session.query(func.avg(ViewerRating.score))
        .filter(ViewerRating.track_id == track_id)
        .scalar()
    )

    viewer_crit_rows = (
        db.session.query(
            ViewerRating.criterion_key,
            func.avg(ViewerRating.score).label("avg_score"),
        )
        .filter(ViewerRating.track_id == track_id)
        .group_by(ViewerRating.criterion_key)
        .all()
    )

    viewer_criteria = []
    for key, label in CRITERIA:
        row = next((r for r in viewer_crit_rows if r.criterion_key == key), None)
        avg_val = float(row.avg_score) if row and row.avg_score is not None else None
        viewer_criteria.append(
            {"key": key, "label": label, "avg": avg_val}
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
        "viewer_criteria": viewer_criteria,
    }
    return jsonify(payload)



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
    if not _require_admin():
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
    if not _require_admin():
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
    try:
        with state_lock:
            if shared_state.get("active_submission_id") == sid:
                shared_state["active_submission_id"] = None
                shared_state["playback"] = {
                    "is_playing": False,
                    "position_ms": 0,
                    "server_ts_ms": _now_ms(),
                }
                shared_state["track_name"] = ""
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
    if not _require_admin():
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

    # снимем флаг playing с предыдущего (если он не оценён)
    try:
        prev_playing = (
            db.session.query(TrackSubmission)
            .filter(TrackSubmission.status == "playing")
            .all()
        )
        for p in prev_playing:
            if p.id != sub.id:
                if p.linked_track_id:
                    p.status = "done"
                else:
                    p.status = "queued"
        sub.status = "playing"
        db.session.commit()
    except Exception:
        pass

    track_name = _submission_display_name(sub)
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
    if not _require_admin():
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
    rater_id = (data or {}).get("rater_id")
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
    if not _require_admin():
        return
    global _next_rater_id
    with state_lock:
        rid = str(_next_rater_id)
        _next_rater_id += 1
        order = len(shared_state["raters"])
        new_rater = {
            "id": rid,
            "name": f"Оценщик {order + 1}",
            "order": order,
            "scores": {key: 0 for key, _ in CRITERIA},
        }
        shared_state["raters"][rid] = new_rater
        payload = {"rater": new_rater}
    emit("rater_added", payload)


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

    with state_lock:
        track_name = shared_state["track_name"] or "Без названия"
        active_submission_id = shared_state.get("active_submission_id")
        raters_list = list(shared_state["raters"].values())
        raters_list.sort(key=lambda r: r.get("order", 0))

    if not raters_list:
        return

    track = Track(name=track_name)
    if active_submission_id:
        try:
            track.submission_id = int(active_submission_id)
        except Exception:
            track.submission_id = None
    db.session.add(track)
    db.session.flush()

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
    emit("evaluation_result", payload)


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


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
