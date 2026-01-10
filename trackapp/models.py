"""SQLAlchemy models and lightweight in-place migrations.

This module was extracted from the historical single-file app.
"""

from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

from sqlalchemy import text

from .extensions import app, db, ADMIN_USERNAME, ADMIN_PASSWORD

class News(db.Model):
    __tablename__ = "news_items"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    text = db.Column(db.Text, nullable=True)
    tag = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)



class NewsAttachment(db.Model):
    __tablename__ = "news_attachments"

    id = db.Column(db.Integer, primary_key=True)
    news_id = db.Column(db.Integer, db.ForeignKey("news_items.id"), nullable=False, index=True)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    news = db.relationship(
        "News",
        backref=db.backref("attachments", lazy="select", cascade="all, delete-orphan"),
    )


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
        index=True,  # Frequently filtered by status
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

    # Telegram бот (кто отправил)
    tg_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    tg_username = db.Column(db.String(64), nullable=True)

    # Платёж (Stars / DonationAlerts) — фиксируем для идемпотентности
    payment_status = db.Column(db.String(16), nullable=False, default="none")  # none|pending|paid
    payment_provider = db.Column(db.String(32), nullable=True)  # stars|donationalerts
    payment_ref = db.Column(db.String(128), nullable=True)
    payment_amount = db.Column(db.Integer, nullable=True)


class Track(db.Model):
    __tablename__ = "tracks"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)  # Frequently filtered
    # Если трек пришёл из очереди (загрузка зрителем) — ссылка на submission.
    submission_id = db.Column(db.Integer, db.ForeignKey("track_submissions.id"), nullable=True)


class Evaluation(db.Model):
    __tablename__ = "evaluations"
    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), nullable=False, index=True)  # FK index
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


class TrackReview(db.Model):
    """Рецензия пользователя на трек.

    Продуктовое решение:
    - 1 пользователь = 1 рецензия на 1 трек (уникальность)
    - рецензия = текст + оценка
    """

    __tablename__ = "track_reviews"

    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)

    # Legacy column from earlier schema versions (NOT NULL in existing SQLite DB).
    # We keep it for backward compatibility and store a rounded version of `overall` here.
    rating = db.Column(db.Integer, nullable=False, default=0)

    # Средний общий балл по всем критериям (0..10), вычисляется при сохранении.
    overall = db.Column(db.Float, nullable=False, default=0.0)
    text = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("track_id", "user_id", name="ux_track_reviews_track_user"),
    )

    user = db.relationship("User", lazy="joined")


class TrackReviewScore(db.Model):
    """Оценки по критериям для пользовательской рецензии."""

    __tablename__ = "track_review_scores"

    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.Integer, db.ForeignKey("track_reviews.id"), index=True, nullable=False)
    criterion_key = db.Column(db.String(50), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("review_id", "criterion_key", name="ux_track_review_scores_review_criterion"),
    )

    review = db.relationship(
        "TrackReview",
        backref=db.backref("scores", lazy="select", cascade="all, delete-orphan"),
    )





class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    # Historically this field stored a plaintext password.
    # Going forward we store a Werkzeug password hash in this column.
    password = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(64), nullable=True)
    email = db.Column(db.String(255), unique=True, nullable=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    # IMPORTANT: default must be a regular user.
    # Admins/superadmins are created explicitly (see bottom of this module).
    role = db.Column(db.String(16), nullable=False, default="user")  # user|judge|admin|superadmin
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # -----------------
    # Password helpers
    # -----------------
    def has_password_hash(self) -> bool:
        """Best-effort detection of whether `password` looks like a Werkzeug hash."""
        p = (self.password or "")
        # Werkzeug hashes are typically like: "pbkdf2:sha256:..." or "scrypt:...".
        return p.startswith("pbkdf2:") or p.startswith("scrypt:") or p.startswith("argon2:")

    def check_password(self, raw_password: str) -> bool:
        if not raw_password:
            return False
        if self.has_password_hash():
            try:
                return check_password_hash(self.password, raw_password)
            except Exception:
                return False
        # Legacy plaintext compare (will be upgraded on successful login).
        return self.password == raw_password

    def set_password(self, raw_password: str):
        self.password = generate_password_hash(raw_password)
        self.password_changed_at = datetime.utcnow()

    # -----------------
    # Email helpers
    # -----------------
    def is_email_verified(self) -> bool:
        return self.email is not None and self.email_verified_at is not None

    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")


    def is_judge(self) -> bool:
        return self.role in ("judge", "admin", "superadmin")




class EmailVerificationToken(db.Model):
    __tablename__ = "email_verification_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, index=True)  # sha256 hex
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("email_verification_tokens", lazy="dynamic"))

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.utcnow()


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, index=True)  # sha256 hex
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("password_reset_tokens", lazy="dynamic"))

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.utcnow()


# -----------------
# Awards (Премии)
# -----------------


class Award(db.Model):
    """Award created from the UI (admin).

    Mechanics:
    - Left column: list of awards (CRUD)
    - Center: nominees (tracks) + listen + set winner
    - Right: fixed winner block (snapshot)
    """

    __tablename__ = "awards"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # Emoji badge used in top/track pages (instead of long text)
    icon_emoji = db.Column(db.String(16), nullable=True)
    # Optional image displayed in awards list / nomination list
    image_path = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="active", index=True)  # draft|active|ended - frequently filtered
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    winner_nomination_id = db.Column(db.Integer, db.ForeignKey("award_nominations.id"), nullable=True)
    # Snapshot for stable display even if the track changes/deletes later.
    # Store JSON as TEXT for SQLite compatibility.
    winner_snapshot_json = db.Column(db.Text, nullable=True)

    created_by = db.relationship("User", lazy="joined", foreign_keys=[created_by_user_id])

    # There are TWO FK paths between awards and award_nominations:
    # - AwardNomination.award_id -> Award.id
    # - Award.winner_nomination_id -> AwardNomination.id
    # SQLAlchemy needs an explicit hint for relationships to avoid ambiguity.
    winner_nomination = db.relationship(
        "AwardNomination",
        foreign_keys=[winner_nomination_id],
        lazy="joined",
        post_update=True,
    )


class AwardNomination(db.Model):
    __tablename__ = "award_nominations"

    id = db.Column(db.Integer, primary_key=True)
    award_id = db.Column(db.Integer, db.ForeignKey("awards.id"), nullable=False, index=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), nullable=False, index=True)
    nominated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    nominated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("award_id", "track_id", name="ux_award_nominations_award_track"),
    )

    award = db.relationship(
        "Award",
        foreign_keys=[award_id],
        backref=db.backref(
            "nominations",
            lazy="dynamic",
            cascade="all, delete-orphan",
            # Make the backref also use the correct FK path.
            foreign_keys="AwardNomination.award_id",
        ),
    )
    track = db.relationship("Track", lazy="joined")
    nominated_by = db.relationship("User", lazy="joined", foreign_keys=[nominated_by_user_id])

def _sqlite_has_column(table: str, col: str) -> bool:
    """Check column existence for in-place sqlite migrations."""
    try:
        rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
        # rows: (cid, name, type, notnull, dflt_value, pk)
        return any(r[1] == col for r in rows)
    except Exception:
        return False


def _sqlite_add_column(table: str, col: str, col_def: str):
    """ALTER TABLE ... ADD COLUMN for sqlite (best-effort)."""
    if _sqlite_has_column(table, col):
        return
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"))


def _run_sqlite_migrations():
    """Lightweight migrations for existing sqlite DB files.

    `db.create_all()` does NOT alter existing tables, so we add new columns
    manually when running against an old DB.
    """
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not (uri or "").startswith("sqlite:"):
        return

    # Users table extensions (profile/email/security)
    _sqlite_add_column("users", "display_name", "VARCHAR(64)")
    _sqlite_add_column("users", "email", "VARCHAR(255)")
    _sqlite_add_column("users", "email_verified_at", "DATETIME")
    _sqlite_add_column("users", "session_version", "INTEGER NOT NULL DEFAULT 1")
    _sqlite_add_column("users", "password_changed_at", "DATETIME")

    # Unique index for email (sqlite allows multiple NULLs in UNIQUE index)
    try:
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email)"))
    except Exception:
        pass

    db.session.commit()

    # Reviews: add computed overall column if updating from an older patch
    try:
        _sqlite_add_column("track_reviews", "overall", "FLOAT NOT NULL DEFAULT 0.0")
        # Backfill overall from legacy `rating` column if present
        try:
            cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(track_reviews)")).fetchall()]
            if "rating" in cols:
                db.session.execute(
                    text("UPDATE track_reviews SET overall = rating WHERE (overall IS NULL OR overall = 0.0) AND rating IS NOT NULL AND rating != 0")
                )
                db.session.commit()
        except Exception:
            pass
    except Exception:
        pass

    # Ensure review scores table exists (older DBs won't have it)
    try:
        db.session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS track_review_scores (
                    id INTEGER PRIMARY KEY,
                    review_id INTEGER NOT NULL,
                    criterion_key VARCHAR(50) NOT NULL,
                    score INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ux_track_review_scores_review_criterion UNIQUE (review_id, criterion_key),
                    FOREIGN KEY(review_id) REFERENCES track_reviews (id)
                )
                """
            )
        )
        db.session.commit()
    except Exception:
        pass

    # Awards UI: add columns if DB existed before awards patch
    try:
        _sqlite_add_column("awards", "icon_emoji", "VARCHAR(16)")
        _sqlite_add_column("awards", "image_path", "TEXT")
        db.session.commit()
    except Exception:
        pass

    # Performance indexes for frequently filtered columns (SQLite)
    # These are safe to run multiple times (IF NOT EXISTS)
    try:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_track_submissions_status ON track_submissions(status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_tracks_is_deleted ON tracks(is_deleted)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_evaluations_track_id ON evaluations(track_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_awards_status ON awards(status)"))
        db.session.commit()
    except Exception as e:
        print("Warning: could not create performance indexes:", e)




def _ensure_submission_tg_columns():
    """Add tg_user_id, tg_username, payment_* columns if missing (SQLite-safe)."""
    try:
        rows = db.session.execute(text("PRAGMA table_info(track_submissions)")).fetchall()
        cols = [r[1] for r in rows]
        if "tg_user_id" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN tg_user_id BIGINT"))
        if "tg_username" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN tg_username VARCHAR(64)"))
        if "payment_status" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN payment_status VARCHAR(16)"))
            db.session.execute(text("UPDATE track_submissions SET payment_status = 'none' WHERE payment_status IS NULL"))
        if "payment_provider" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN payment_provider VARCHAR(32)"))
        if "payment_ref" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN payment_ref VARCHAR(128)"))
        if "payment_amount" not in cols:
            db.session.execute(text("ALTER TABLE track_submissions ADD COLUMN payment_amount INTEGER"))
        db.session.commit()
    except Exception as e:
        print("Warning: could not ensure TrackSubmission TG/payment columns:", e)


with app.app_context():
    db.create_all()
    _run_sqlite_migrations()

    # --- Lightweight in-place migrations for new User fields ---
    try:
        rows = db.session.execute(text("PRAGMA table_info(users)")).fetchall()
        cols = [r[1] for r in rows]
        if "display_name" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN display_name TEXT"))
            db.session.commit()
        if "session_version" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"))
            db.session.commit()
        if "password_changed_at" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN password_changed_at DATETIME"))
            db.session.commit()
    except Exception as e:
        print("Warning: could not ensure user profile columns:", e)
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

    _ensure_submission_tg_columns()

    

# инициализация супер-админа, если пользователей ещё нет
    try:
        if not db.session.query(User).count():
            sa = User(
                username=ADMIN_USERNAME,
                # Store password as a hash (previous versions stored plaintext)
                password=generate_password_hash(ADMIN_PASSWORD),
                role="superadmin",
            )
            db.session.add(sa)
            db.session.commit()
            print(f"Created default superadmin user: {ADMIN_USERNAME}")
    except Exception as e:
        print("Warning: could not ensure default superadmin:", e)
