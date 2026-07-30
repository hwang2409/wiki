"""Common error type + constants for the replay package.

Kept in a tiny module so both ``reader.py`` and ``service.py`` can raise
consistently without pulling in each other's imports.
"""

from __future__ import annotations

import re


RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Bookmark rendering budget: keep the count small so a scrubber's tick track
# stays legible on a 480px panel even for long runs. Truncation is surfaced
# via ``bookmarks_truncated`` so nothing is silently hidden.
MAX_BOOKMARKS = 200
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

# Per-line hard ceiling. Real supervisor events observed on disk are ~200 B –
# 30 KiB; anything above 1 MiB is corruption or an attack and the reader
# discards it wholesale instead of buffering.
MAX_LINE_BYTES = 1 * 1024 * 1024

# Per-scan hard ceiling. Every byte physically read counts — including bytes
# consumed while skipping an oversized record — so a 100 GiB file terminates
# in constant peak RSS. Pagination resumes across scan-truncated pages via
# the ``skipping`` field of the cursor, so no event is unreachable.
MAX_SCAN_BYTES = 64 * 1024 * 1024

# Per-run.json read cap. Real ``run.json`` files are ~14 KiB even with a
# long ``initial_prompt``. 256 KiB gives ample headroom without letting a
# corrupt or adversarial metadata file drive unbounded reads.
MAX_RUN_JSON_BYTES = 256 * 1024

# ``resolve_ticket_runs`` outer caps.
MAX_RUN_LIST_SCAN = 500
MAX_RUN_LIST_ENTRIES = 200

# Chunked stream reads: 128 KiB balances syscall overhead against per-response
# RSS. We never buffer more than one chunk beyond the current partial record.
STREAM_CHUNK = 128 * 1024


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id))


class ReplayError(Exception):
    """Raised when a run directory is unreadable, malformed, or refused.

    ``status_code`` maps directly to the endpoint HTTP status.
    """

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code
