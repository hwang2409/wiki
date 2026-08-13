"""`wiki tui` — read-only terminal UI for the local sidecar fleet.

Ships inside the wiki CLI with zero new deps: pure ``curses`` + stdlib
HTTP. Consumes the same ``/dashboard/data`` payload the browser
dashboard renders, plus ``/api/agents/{ticket}/session`` for the worker
detail transcript.
"""

from .app import main

__all__ = ["main"]
