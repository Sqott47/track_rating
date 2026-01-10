"""TrackRaterAntigaz application package.

Public exports:
- app: Flask instance
- socketio: SocketIO instance

The actual routes and Socket.IO events are imported for side-effects.
"""

from .core import app, socketio, db  # noqa: F401

# Register routes from modular package (all routes now in routes/ submodules)
from . import routes  # noqa: F401

# Register socket handlers
from . import sockets  # noqa: F401


