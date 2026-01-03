"""Compatibility re-export module.

The project was historically a single app.py. During refactor we split the code into
multiple modules but keep a stable import surface so legacy modules can still do
`from .core import ...`.

This module re-exports public symbols from:
- extensions.py (app/db/socketio, config/constants, storage helpers)
- models.py (SQLAlchemy models and migrations)
- state.py (shared in-memory state, serialization, broadcast helpers, auth helpers)
"""

from .extensions import *  # noqa: F401,F403
from .models import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403

# --- Explicit re-export of underscore helpers ---
# Star-import does not pull names that start with '_' unless they are listed in __all__.
# Our legacy modules import a few '_' helpers from .core explicitly, so we re-export them here.
from .extensions import (
    _get_or_create_viewer_id,
    _get_s3_client,
    _s3_is_configured,
    _s3_key_for_submission,
)
from .state import (
    _broadcast_playback_state,
    _broadcast_queue_state,
    _compute_playback_position_ms,
    _get_playback_snapshot,
    _get_track_url,
    _init_default_raters,
    _is_image_filename,
    _is_safe_uuid,
    _now_ms,
    _require_admin,
    _require_panel_access,
    _require_superadmin,
    _serialize_queue_state,
    _serialize_state,
    _submission_display_name,
)

