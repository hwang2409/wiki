"""Conflict-marker parsing and safe line-ending resolution.

The parser only accepts Git's exact seven-character conflict markers.  Any
other marker-like line raises ``RebaseError`` rather than falling through as
"clean" data — a permissive parser is how binary or malformed conflicts
silently commit ``ours`` and lose upstream changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class RebaseError(RuntimeError):
    """A rebase operation could not be completed safely."""


_CONFLICT_START = re.compile(r"^<<<<<<<(?:\s.*)?$")
_CONFLICT_MID = re.compile(r"^=======$")
_CONFLICT_BASE = re.compile(r"^\|{7}(?:\s.*)?$")
_CONFLICT_END = re.compile(r"^>>>>>>>(?:\s.*)?$")
_CONFLICT_LIKE = re.compile(r"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?:\s.*)?$")


def _line_ending_only(line: str) -> str:
    return line.replace("\r\n", "\n").replace("\r", "\n")


def _parse_segments(text: str) -> list[Any]:
    """Return an alternating list of literal-line runs and conflict hunks.

    Each element is either ``("text", lines)`` for pass-through content or
    ``("hunk", ours, base, theirs)`` for a Git conflict block.  Marker-like
    lines with a different marker size raise ``RebaseError`` so malformed
    conflicts never fall through as "clean".
    """

    def marker(line: str) -> str | None:
        if _CONFLICT_START.fullmatch(line):
            return "start"
        if _CONFLICT_BASE.fullmatch(line):
            return "base"
        if _CONFLICT_MID.fullmatch(line):
            return "middle"
        if _CONFLICT_END.fullmatch(line):
            return "end"
        if _CONFLICT_LIKE.fullmatch(line):
            raise RebaseError("malformed conflict marker")
        return None

    lines = text.splitlines(keepends=True)
    segments: list[Any] = []
    current_text: list[str] = []
    index = 0
    while index < len(lines):
        kind = marker(lines[index].rstrip("\r\n"))
        if kind is None:
            current_text.append(lines[index])
            index += 1
            continue
        if kind != "start":
            raise RebaseError("unexpected conflict marker")
        if current_text:
            segments.append(("text", current_text))
            current_text = []
        index += 1
        ours: list[str] = []
        while index < len(lines):
            m = marker(lines[index].rstrip("\r\n"))
            if m in {"middle", "base"}:
                break
            if m is not None:
                raise RebaseError("nested or misplaced conflict marker")
            ours.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        base: list[str] | None = None
        if marker(lines[index].rstrip("\r\n")) == "base":
            index += 1
            base = []
            while index < len(lines):
                m = marker(lines[index].rstrip("\r\n"))
                if m == "middle":
                    break
                if m is not None:
                    raise RebaseError("nested or misplaced conflict marker")
                base.append(lines[index])
                index += 1
            if index >= len(lines):
                raise RebaseError("incomplete diff3 conflict hunk")
        # skip =======
        index += 1
        theirs: list[str] = []
        while index < len(lines):
            m = marker(lines[index].rstrip("\r\n"))
            if m == "end":
                break
            if m is not None:
                raise RebaseError("nested or misplaced conflict marker")
            theirs.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        # skip >>>>>>>
        index += 1
        segments.append(("hunk", ours, base, theirs))
    if current_text:
        segments.append(("text", current_text))
    return segments


def _parse_conflicts(
    text: str,
) -> tuple[list[tuple[list[str], list[str] | None, list[str]]], bool]:
    """Compat wrapper: return ``(hunks, found)`` from the segment parser."""

    segments = _parse_segments(text)
    hunks: list[tuple[list[str], list[str] | None, list[str]]] = []
    for segment in segments:
        if segment[0] == "hunk":
            _kind, ours, base, theirs = segment
            hunks.append((ours, base, theirs))
    return hunks, bool(hunks)


def resolve_conflict_file(path: Path) -> tuple[bool, str | None]:
    """Resolve only conflicts whose sides differ in line endings.

    Returns ``(resolved, summary)``.  A false result never writes the file.
    Files without any conflict markers ARE NOT considered resolved: the
    caller was told the file is unmerged, and a text file with no markers
    (or a binary blob read as text) must escalate rather than commit ``ours``
    unchanged.
    """

    raw = path.read_text(encoding="utf-8", errors="surrogateescape")
    try:
        segments = _parse_segments(raw)
    except RebaseError as exc:
        return False, str(exc)
    hunks = [segment for segment in segments if segment[0] == "hunk"]
    if not hunks:
        return False, "no conflict markers present"

    for _kind, ours, _base, theirs in hunks:
        if [_line_ending_only(line) for line in ours] != [
            _line_ending_only(line) for line in theirs
        ]:
            excerpt = "".join(
                ["<<<<<<< ours\n", *ours, "=======\n", *theirs, ">>>>>>> theirs\n"]
            )
            return False, excerpt.strip()[:1200]

    output: list[str] = []
    for segment in segments:
        if segment[0] == "text":
            output.extend(segment[1])
        else:
            _kind, ours, _base, _theirs = segment
            output.extend(_line_ending_only(line) for line in ours)
    normalized = "".join(output).replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(normalized, encoding="utf-8", errors="surrogateescape", newline="")
    return True, None
