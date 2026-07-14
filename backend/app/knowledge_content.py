"""Pure content parsing helpers for the Wiki knowledge index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


DEFAULT_CHUNK_CHARS = 4_000
WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


@dataclass(frozen=True)
class NoteChunk:
    heading: str | None
    text: str
    pos: int


def frontmatter_body(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return {}, content


def _fence_marker(line: str) -> str | None:
    match = FENCE_RE.match(line)
    return match.group(1) if match else None


def _split_plain_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def _section_units(lines: list[str], max_chars: int) -> list[str]:
    """Return paragraph/fence-safe units, splitting only plain oversized text."""

    units: list[tuple[str, bool]] = []
    plain: list[str] = []
    fenced: list[str] = []
    fence: str | None = None

    def flush_plain() -> None:
        if not plain:
            return
        value = "\n".join(plain).strip()
        plain.clear()
        if value:
            units.extend((piece, False) for piece in _split_plain_text(value, max_chars))

    for line in lines:
        marker = _fence_marker(line)
        if fence is not None:
            fenced.append(line)
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                units.append(("\n".join(fenced).strip(), True))
                fenced = []
                fence = None
            continue
        if marker:
            flush_plain()
            fence = marker
            fenced = [line]
            continue
        if not line.strip():
            flush_plain()
            continue
        plain.append(line)
    flush_plain()
    if fenced:
        # An unterminated fence is still one indivisible source block.
        units.append(("\n".join(fenced).strip(), True))

    chunks: list[str] = []
    current = ""
    for unit, is_fence in units:
        if not unit:
            continue
        candidate = unit if not current else f"{current}\n\n{unit}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        elif is_fence and len(unit) > max_chars:
            if current:
                chunks.append(current)
            chunks.append(unit)
            current = ""
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def chunk_markdown(
    content: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[NoteChunk]:
    """Chunk Markdown without crossing headings or splitting fenced blocks."""

    if max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    _, body = frontmatter_body(content)
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        marker = _fence_marker(line)
        if fence is not None:
            lines.append(line)
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker
            lines.append(line)
            continue
        match = HEADING_RE.match(line)
        if match:
            if lines or heading is not None:
                sections.append((heading, lines))
            heading = match.group(2).strip()
            lines = []
            continue
        lines.append(line)
    if lines or heading is not None or not sections:
        sections.append((heading, lines))

    chunks: list[NoteChunk] = []
    for section_heading, section_lines in sections:
        values = _section_units(section_lines, max_chars)
        if not values and section_heading:
            values = [""]
        for value in values:
            chunks.append(NoteChunk(section_heading, value, len(chunks)))
    return chunks


def strip_code(content: str) -> str:
    """Remove fenced and inline code before extracting wikilinks."""

    output: list[str] = []
    fence: str | None = None
    for line in content.splitlines():
        marker = _fence_marker(line)
        if fence is not None:
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker
            continue
        output.append(re.sub(r"`[^`\n]*`", "", line))
    return "\n".join(output)


def normalize_link_target(target: str) -> str:
    base = target.strip().partition("#")[0].strip().replace("\\", "/")
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base.strip("/")


def resolve_wikilink_target(target: str, note_paths: Iterable[str]) -> str | None:
    """Resolve one Obsidian wikilink target; ambiguous basenames stay unresolved."""

    normalized = normalize_link_target(target)
    if not normalized:
        return None
    paths = sorted({Path(path).as_posix() for path in note_paths})
    by_no_suffix = {
        Path(path).with_suffix("").as_posix().casefold(): path for path in paths
    }
    exact = by_no_suffix.get(normalized.casefold())
    if exact:
        return exact
    matches = [path for path in paths if Path(path).stem.casefold() == normalized.casefold()]
    return matches[0] if len(matches) == 1 else None


def extract_wikilinks(content: str) -> list[str]:
    return [match.group(1).strip() for match in WIKILINK_RE.finditer(strip_code(content))]


def extract_title(content: str, rel_path: str) -> str:
    _, body = frontmatter_body(content)
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return Path(rel_path).stem.replace("-", " ").replace("_", " ").title()


def ticket_for_note(rel_path: str, content: str) -> str | None:
    match = TICKET_RE.search(f"{rel_path}\n{content}")
    return match.group(0) if match else None


def _json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"encrypted_content", "data_base64"}:
                continue
            yield from _json_strings(item)


def event_text(event: Mapping[str, Any]) -> str:
    """Flatten normalized event payload strings into searchable transcript text."""

    kind = str(event.get("kind") or "unknown")
    strings = [kind]
    seen = {kind}
    for value in _json_strings(event.get("payload") or {}):
        if value in seen:
            continue
        seen.add(value)
        strings.append(value)
    return "\n".join(strings)
