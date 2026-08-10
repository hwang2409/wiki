"""Session replay timeline builder for archived agent runs (WIKI-174).

The package is split by responsibility so future changes stay local:

* ``errors``     — the ``ReplayError`` type, module constants, ``valid_run_id``.
* ``models``     — dataclasses shared across the reader, service, and metadata.
* ``cursor``     — HMAC-signed pagination cursors bound to a run_id.
* ``reader``     — ``SnapshotReader`` + fd-safe child opens (O_NONBLOCK,
                    S_ISREG check, symlink refusal via pathwalk).
* ``classification`` — bookmark + summary derivation from event payloads.
* ``metadata``   — bounded ``run.json`` reads + ticket→runs discovery.
* ``service``    — response builders that ``main.py`` calls per-endpoint.

This ``__init__`` re-exports every public name that the rest of the app
(``main.py``) and the tests need, so external imports continue to look
like ``from backend.app import replay; replay.build_timeline_response(...)``.
"""

from __future__ import annotations

from .cursor import (
    _reset_secret_for_tests,
    decode_cursor,
    encode_cursor,
)
from .errors import (
    DEFAULT_LIMIT,
    MAX_BOOKMARKS,
    MAX_LIMIT,
    MAX_LINE_BYTES,
    MAX_RUN_JSON_BYTES,
    MAX_RUN_LIST_ENTRIES,
    MAX_RUN_LIST_SCAN,
    MAX_SCAN_BYTES,
    RUN_ID_PATTERN,
    ReplayError,
    valid_run_id,
)
from .metadata import (
    _read_bounded_metadata,
    _run_summary_from_meta,
    build_run_summary,
    build_run_summary_from_run_fd,
    resolve_ticket_runs,
)
from .models import (
    RunSummary,
    ScanStats,
    TicketRunsListing,
    TimelineEvent,
    TimelinePage,
)
from .reader import (
    SnapshotReader,
    _open_run_child_fd,
    _open_run_file_fd,
    open_run_dir_fd,
    open_runs_root_fd,
    verify_run_dir_exists,
)
from .service import (
    _build_bookmarks,
    _build_timeline_page,
    _warnings_from,
    build_timeline_response,
    build_timeline_response_from_run_fd,
    load_raw_event,
    load_raw_event_from_run_fd,
)

# Legacy alias — the round-3 code referred to ``_ScanStats``; the class was
# renamed to ``ScanStats`` when it moved to ``models``. Kept so external
# test files that patched ``replay._ScanStats`` continue to work.
_ScanStats = ScanStats

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_BOOKMARKS",
    "MAX_LIMIT",
    "MAX_LINE_BYTES",
    "MAX_RUN_JSON_BYTES",
    "MAX_RUN_LIST_ENTRIES",
    "MAX_RUN_LIST_SCAN",
    "MAX_SCAN_BYTES",
    "ReplayError",
    "RUN_ID_PATTERN",
    "RunSummary",
    "ScanStats",
    "SnapshotReader",
    "TicketRunsListing",
    "TimelineEvent",
    "TimelinePage",
    "build_run_summary",
    "build_run_summary_from_run_fd",
    "build_timeline_response",
    "build_timeline_response_from_run_fd",
    "decode_cursor",
    "encode_cursor",
    "load_raw_event",
    "load_raw_event_from_run_fd",
    "open_run_dir_fd",
    "open_runs_root_fd",
    "resolve_ticket_runs",
    "valid_run_id",
    "verify_run_dir_exists",
]
