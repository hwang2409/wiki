"""HMAC-signed pagination cursors bound to a specific run.

Round-5 review item 1: forged/tampered cursors used to skip records or
return 200 past EOF. The cursor now:

* Carries the ``run_id`` inside the signed payload — a cursor from run A
  cannot be replayed against run B.
* Encodes ``byte_offset`` and ``skipping`` as typed fields; any non-int
  offset, negative offset, non-bool skipping, or missing/wrong-type
  ``run_id`` field decodes to 400.
* Is signed with HMAC-SHA256 under a per-process secret; a tampered
  payload fails ``compare_digest`` and decodes to 400.
* Callers ALSO enforce ``offset <= snapshot_size`` at read time so a
  legitimately-signed cursor pointing past EOF (should not happen — the
  server always signs offsets within its own snapshot) still 400s
  rather than silently returning empty pages.

The secret is generated with ``os.urandom(32)`` at module import. Tests
can rebind it via ``_reset_secret_for_tests`` to build tamper probes.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from hashlib import sha256

from .errors import ReplayError


_CURSOR_SECRET: bytes = os.urandom(32)
_SIG_LEN = 32  # sha256 digest length in bytes


def _reset_secret_for_tests(secret: bytes) -> None:
    """Rebind the HMAC secret. Tests only — never call from production code."""

    global _CURSOR_SECRET
    _CURSOR_SECRET = secret


def _sign(payload: bytes) -> bytes:
    return hmac.new(_CURSOR_SECRET, payload, sha256).digest()


def encode_cursor(byte_offset: int, *, run_id: str, skipping: bool = False) -> str:
    """Encode a resumable position + skip flag into an opaque signed token.

    The ``run_id`` is stapled inside the signed payload so a cursor cannot
    be replayed across runs. Clients MUST NOT parse or transform this
    token; the encoding is not part of the API contract.
    """

    if not isinstance(byte_offset, int) or byte_offset < 0:
        raise ReplayError("cursor offset must be a non-negative int", status_code=500)
    if not isinstance(run_id, str) or not run_id:
        raise ReplayError("cursor run_id must be a non-empty string", status_code=500)
    payload_dict = {
        "o": int(byte_offset),
        "s": bool(skipping),
        "r": run_id,
    }
    payload = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign(payload)
    # Concatenate payload then signature. Signature is a fixed SHA-256
    # length; ``decode`` splits on that boundary instead of a delimiter,
    # because a byte matching an ASCII delimiter can appear inside the
    # signature and break rsplit-based parsing.
    envelope = payload + signature
    return base64.urlsafe_b64encode(envelope).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None, *, run_id: str) -> tuple[int, bool]:
    """Return ``(byte_offset, skipping)`` for a legitimate cursor.

    A missing / empty cursor is treated as the resume point ``(0, False)``.
    Any other failure — malformed base64, malformed JSON, signature
    mismatch, wrong ``run_id``, or bad field types — raises
    ``ReplayError(status_code=400)``. Callers do NOT need to distinguish
    the failure modes; the error surface is deliberately uniform so
    probes can't fingerprint which check tripped.
    """

    if cursor is None or cursor == "":
        return 0, False
    if not isinstance(cursor, str):
        raise ReplayError("invalid cursor", status_code=400)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ReplayError("invalid cursor", status_code=400) from exc
    if len(raw) <= _SIG_LEN:
        raise ReplayError("invalid cursor", status_code=400)
    payload, signature = raw[:-_SIG_LEN], raw[-_SIG_LEN:]
    expected = _sign(payload)
    if not hmac.compare_digest(expected, signature):
        raise ReplayError("invalid cursor", status_code=400)
    try:
        value = json.loads(payload)
    except ValueError as exc:
        raise ReplayError("invalid cursor", status_code=400) from exc
    if not isinstance(value, dict):
        raise ReplayError("invalid cursor", status_code=400)
    offset = value.get("o")
    skipping = value.get("s")
    cursor_run_id = value.get("r")
    # ``isinstance(True, int)`` is True in Python; guard against bool-as-int.
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ReplayError("invalid cursor", status_code=400)
    if not isinstance(skipping, bool):
        raise ReplayError("invalid cursor", status_code=400)
    if not isinstance(cursor_run_id, str) or cursor_run_id != run_id:
        raise ReplayError("invalid cursor", status_code=400)
    return int(offset), skipping
