"""Enumerate installed macOS fonts and expose safe server-side file ids."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypedDict


logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_CACHE: list[FontFamily] | None = None
_FILE_MAP: dict[str, FontLocation] = {}
_EXTRACTED_PATHS: dict[str, FontLocation] = {}
_FONT_ROOTS = tuple(Path.home() / part for part in ("Library/Fonts",)) + (
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
)
_FONT_ROOT_COMPONENTS = tuple(
    tuple(part for part in root.expanduser().parts if part != "/") for root in _FONT_ROOTS
)
_FONT_SUFFIXES = {".ttf", ".otf"}
MAX_FONT_FILE_BYTES = 50 * 1024 * 1024


class FontFile(TypedDict, total=False):
    id: str
    weight: int
    style: str


class FontFamily(TypedDict):
    family: str
    files: list[FontFile]


@dataclass(frozen=True)
class FontLocation:
    root_id: int
    relative_parts: tuple[str, ...]
    suffix: str


class FontFileTooLarge(Exception):
    """The opened font file exceeds the serving limit."""


@dataclass
class OpenedFontFile:
    stream: BinaryIO
    suffix: str
    size: int


def _allowed_font_path(path: Path) -> Path | None:
    """Return a real font path only when it stays in an approved root."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix.lower() not in _FONT_SUFFIXES:
        return None
    for root in _FONT_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    return None


def _font_location(path: Path) -> FontLocation | None:
    """Convert an enumerated path to static root and relative components."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    for root_id, root in enumerate(_FONT_ROOTS):
        try:
            root_path = root.expanduser().resolve(strict=True)
            relative = resolved.relative_to(root_path)
        except (OSError, ValueError):
            continue
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            continue
        return FontLocation(root_id=root_id, relative_parts=relative.parts, suffix=resolved.suffix.casefold())
    return None


def _font_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]


def _weight(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if 1 <= number <= 1000 else None
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    try:
        number = int(text)
        return number if 1 <= number <= 1000 else None
    except ValueError:
        pass
    compact = "".join(character for character in text if character.isalnum())
    for suffix in ("italic", "oblique"):
        if compact.endswith(suffix):
            compact = compact[: -len(suffix)]
            break
    # Scan longer aliases first so ``ultralight`` does not match ``light``.
    aliases = (
        ("ultralight", 200),
        ("extralight", 200),
        ("ultrabold", 800),
        ("extrabold", 800),
        ("semibold", 600),
        ("demibold", 600),
        ("hairline", 100),
        ("regular", 400),
        ("normal", 400),
        ("medium", 500),
        ("heavy", 900),
        ("black", 900),
        ("thin", 100),
        ("light", 300),
        ("bold", 700),
        ("book", 350),
    )
    for alias, weight in aliases:
        if alias in compact:
            return weight
    return None


def _typeface_weight(typeface: dict[str, Any]) -> int | None:
    for key in ("weight", "font_weight", "fontWeight", "_weight"):
        value = _weight(typeface.get(key))
        if value is not None:
            return value
    return _weight(typeface.get("style"))


def _location(*entries: dict[str, Any]) -> Path | None:
    for entry in entries:
        for key in ("location", "path", "Location", "Path"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                if raw.startswith("file://"):
                    raw = raw[7:]
                return _allowed_font_path(Path(raw))
    return None


def _extract_fonts_with_paths(payload: Any) -> tuple[list[FontFamily], dict[str, FontLocation]]:
    extracted_paths: dict[str, FontLocation] = {}
    grouped: dict[str, FontFamily] = {}
    entries = payload.get("SPFontsDataType", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return [], extracted_paths
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") != "yes":
            continue
        suffix = str(entry.get("_name", "")).casefold()
        typefaces = entry.get("typefaces", []) or []
        if not isinstance(typefaces, list):
            continue
        for typeface in typefaces:
            if not isinstance(typeface, dict) or typeface.get("enabled") != "yes":
                continue
            location = _location(typeface, entry)
            if suffix.endswith(".ttc") or (location is not None and location.suffix.casefold() == ".ttc"):
                logger.warning("skipping unsupported font collection: %s", entry.get("_name"))
                location = None
            family = typeface.get("family")
            if not isinstance(family, str) or not family.strip():
                continue
            family = family.strip()
            result = grouped.setdefault(family, {"family": family, "files": []})
            if location is None:
                continue
            font_location = _font_location(location)
            if font_location is None:
                continue
            font_id = _font_id(location)
            extracted_paths[font_id] = font_location
            font_file: FontFile = {"id": font_id}
            weight = _typeface_weight(typeface)
            style = typeface.get("style")
            if weight is not None:
                font_file["weight"] = weight
            if isinstance(style, str) and style.strip():
                font_file["style"] = style.strip()
            if not any(item == font_file for item in result["files"]):
                result["files"].append(font_file)
    return sorted(grouped.values(), key=lambda item: item["family"].casefold()), extracted_paths


def _extract_fonts(payload: Any) -> list[FontFamily]:
    fonts, _ = _extract_fonts_with_paths(payload)
    return fonts


def _extract_families(payload: Any) -> list[str]:
    """Keep the original family-only helper for callers and old fixtures."""
    return [entry["family"] for entry in _extract_fonts(payload)]


def _enumerate() -> tuple[list[FontFamily], dict[str, FontLocation]]:
    try:
        completed = subprocess.run(
            ["system_profiler", "SPFontsDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [], {}
    if completed.returncode != 0 or not completed.stdout:
        return [], {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], {}
    return _extract_fonts_with_paths(payload)


def _normalise(value: list[FontFamily] | list[str]) -> list[FontFamily]:
    result: list[FontFamily] = []
    for entry in value:
        if isinstance(entry, str):
            result.append({"family": entry, "files": []})
        elif isinstance(entry, dict) and isinstance(entry.get("family"), str):
            files = entry.get("files", [])
            result.append({"family": entry["family"], "files": files if isinstance(files, list) else []})
    return result


def installed_fonts() -> list[FontFamily]:
    global _CACHE, _FILE_MAP
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        # Keep enumeration and map construction under the same lock. The
        # double-check above lets later calls return without running it.
        enumerated = _enumerate()
        if isinstance(enumerated, tuple):
            raw_fonts, extracted_paths = enumerated
        else:
            # Preserve compatibility with old test doubles and callers.
            raw_fonts, extracted_paths = enumerated, {}
        computed = _normalise(raw_fonts)
        file_map: dict[str, FontLocation] = {}
        for entry in computed:
            for font_file in entry["files"]:
                font_id = font_file.get("id")
                if not isinstance(font_id, str):
                    continue
                location = extracted_paths.get(font_id)
                if location is None:
                    continue
                if not 0 <= location.root_id < len(_FONT_ROOT_COMPONENTS):
                    continue
                if location.suffix not in _FONT_SUFFIXES or not location.relative_parts:
                    continue
                file_map[font_id] = location
        _EXTRACTED_PATHS.clear()
        _EXTRACTED_PATHS.update(extracted_paths)
        if _CACHE is None:
            _CACHE = computed
            _FILE_MAP = file_map
        return _CACHE


def installed_families() -> list[str]:
    return [entry["family"] for entry in installed_fonts()]


def _open_at(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
    return os.open(path, flags, dir_fd=dir_fd)


def _open_font_fd(location: FontLocation, flags: int) -> int | None:
    """Open a mapped font beneath a fixed root using descriptor-relative walks."""
    if location.suffix not in _FONT_SUFFIXES:
        return None
    if not 0 <= location.root_id < len(_FONT_ROOT_COMPONENTS):
        return None
    parts = (*_FONT_ROOT_COMPONENTS[location.root_id], *location.relative_parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None

    current_fd: int | None = None
    try:
        anchor_flags = flags | getattr(os, "O_DIRECTORY", 0)
        current_fd = _open_at("/", anchor_flags)
        for index, part in enumerate(parts):
            component_flags = flags
            if index < len(parts) - 1:
                component_flags |= getattr(os, "O_DIRECTORY", 0)
            next_fd = _open_at(part, component_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        if current_fd is not None:
            os.close(current_fd)
        return None


def open_font_file(font_id: str) -> OpenedFontFile | None:
    """Open an allowlisted font and keep the opened descriptor for streaming."""
    installed_fonts()
    with _LOCK:
        location = _FILE_MAP.get(font_id)
    if location is None:
        return None

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = _open_font_fd(location, flags)
    if fd is None:
        return None
    stream: BinaryIO | None = None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            return None
        if metadata.st_size > MAX_FONT_FILE_BYTES:
            raise FontFileTooLarge
        stream = os.fdopen(fd, "rb", closefd=True)
        return OpenedFontFile(stream=stream, suffix=location.suffix, size=metadata.st_size)
    except BaseException:
        if stream is None:
            os.close(fd)
        raise


def reset_cache_for_tests() -> None:
    global _CACHE, _FILE_MAP
    with _LOCK:
        _CACHE = None
        _FILE_MAP = {}
        _EXTRACTED_PATHS.clear()
