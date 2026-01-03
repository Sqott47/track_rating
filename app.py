"""Thin entrypoint for TrackRaterAntigaz.

This file stays small on purpose.
All application code lives in the `trackapp` package.
"""

from trackapp import app, socketio  # noqa: F401


if __name__ == "__main__":
    # `log_output=False` helps suppress noisy Eventlet tracebacks on client disconnects
    # (e.g., during rapid navigation or refresh in the browser).
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, log_output=False)
