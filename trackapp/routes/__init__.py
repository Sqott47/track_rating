"""Routes package -- fully modularized from routes.py.

This package organizes HTTP routes into logical modules:
- auth: login, register, logout, password reset, settings
- public: home, queue, track pages, top tracks, viewers
- admin: admin panel, news, stream config, user management
- api: JSON API endpoints
- tg_bot: Telegram bot private API
- awards: awards CRUD, nominations, winner management

All route modules register their routes via @app.route decorators
when imported.
"""

# Import all route modules to register their @app.route decorators
from . import auth  # noqa: F401
from . import public  # noqa: F401
from . import api  # noqa: F401
from . import tg_bot  # noqa: F401
from . import admin  # noqa: F401
from . import awards  # noqa: F401
