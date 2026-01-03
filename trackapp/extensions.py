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
import uuid
import bleach

from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from sqlalchemy import func, text


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
UPLOAD_DIR = os.getenv(
    "UPLOAD_DIR",
    os.path.join(BASE_DIR, "static", "uploads"),
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

NEWS_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "news")
os.makedirs(NEWS_UPLOAD_DIR, exist_ok=True)

ALLOWED_NEWS_TAGS = [
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "blockquote",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "hr",
]
ALLOWED_NEWS_ATTRS = {
    "a": ["href", "title", "target", "rel"],
}
ALLOWED_NEWS_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_news_html(raw_html: str) -> str:
    cleaned = bleach.clean(
        raw_html or "",
        tags=ALLOWED_NEWS_TAGS + ["a"],
        attributes=ALLOWED_NEWS_ATTRS,
        protocols=ALLOWED_NEWS_PROTOCOLS,
        strip=True,
    )
    cleaned = bleach.linkify(cleaned)
    return cleaned

# -------------------------
# Track submissions (queue)
# -------------------------
# Храним исходники и сконверченные mp3 в подпапках UPLOAD_DIR.
# В проде UPLOAD_DIR обычно указывает на shared/, поэтому всё окажется в shared.
SUBMISSIONS_RAW_DIR = os.path.join(UPLOAD_DIR, "submissions_raw")
os.makedirs(SUBMISSIONS_RAW_DIR, exist_ok=True)
SUBMISSIONS_TMP_DIR = os.path.join(UPLOAD_DIR, "submissions_tmp")
os.makedirs(SUBMISSIONS_TMP_DIR, exist_ok=True)
# SUBMISSIONS_MP3_DIR ранее использовался для перекодирования.
# Сейчас конвертацию отключаем полностью: храним исходники как есть (mp3/wav)

SUBMISSION_MAX_MB = int(os.getenv("SUBMISSION_MAX_MB", "50"))
ALLOWED_SUBMISSION_EXTS = {"mp3", "wav", "flac", "aiff", "aif", "ogg", "m4a"}
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


app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))
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





