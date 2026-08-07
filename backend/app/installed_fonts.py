"""Enumerate installed macOS fonts and expose safe server-side file ids."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, TypedDict


logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_CACHE: list[FontFamily] | None = None
_FILE_MAP: dict[str, Path] = {}
_EXTRACTED_PATHS: dict[str, Path] = {}
_FONT_ROOTS = tuple(Path.home() / part for part in ("Library/Fonts",)) + (
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
)
_FONT_SUFFIXES = {".ttf", ".otf"}


class FontFile(TypedDict, total=False):
    id: str
    weight: int
    style: str


class FontFamily(TypedDict):
    family: str
    files: list[FontFile]


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
    names = {
        "thin": 100,
        "hairline": 100,
        "extra light": 200,
        "ultra light": 200,
        "light": 300,
        "book": 350,
        "regular": 400,
        "normal": 400,
        "medium": 500,
        "semibold": 600,
        "semi bold": 600,
        "demibold": 600,
        "bold": 700,
        "extrabold": 800,
        "extra bold": 800,
        "ultrabold": 800,
        "ultra bold": 800,
        "black": 900,
        "heavy": 900,
    }
    style_name = " ".join(
        part for part in text.replace("-", " ").replace("_", " ").split()
        if part not in {"italic", "oblique"}
    )
    return names.get(text) or names.get(style_name)


def _typeface_weight(typeface: dict[str, Any]) -> int | None:
    for key in ("weight", "font_weight", "fontWeight", "_weight"):
        value = _weight(typeface.get(key))
        if value is not None:
            return value
    return _weight(typeface.get("style"))


def _location(entry: dict[str, Any]) -> Path | None:
    for key in ("Location", "location", "path", "Path"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.startswith("file://"):
                raw = raw[7:]
            return _allowed_font_path(Path(raw))
    return None


def _extract_fonts(payload: Any) -> list[FontFamily]:
    _EXTRACTED_PATHS.clear()
    grouped: dict[str, FontFamily] = {}
    entries = payload.get("SPFontsDataType", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") != "yes":
            continue
        location = _location(entry)
        suffix = str(entry.get("_name", "")).casefold()
        if suffix.endswith(".ttc") or (location is not None and location.suffix.casefold() == ".ttc"):
            logger.warning("skipping unsupported font collection: %s", entry.get("_name"))
            location = None
        typefaces = entry.get("typefaces", []) or []
        if not isinstance(typefaces, list):
            continue
        for typeface in typefaces:
            if not isinstance(typeface, dict) or typeface.get("enabled") != "yes":
                continue
            family = typeface.get("family")
            if not isinstance(family, str) or not family.strip():
                continue
            family = family.strip()
            result = grouped.setdefault(family, {"family": family, "files": []})
            if location is None:
                continue
            font_id = _font_id(location)
            _EXTRACTED_PATHS[font_id] = location
            font_file: FontFile = {"id": font_id}
            weight = _typeface_weight(typeface)
            style = typeface.get("style")
            if weight is not None:
                font_file["weight"] = weight
            if isinstance(style, str) and style.strip():
                font_file["style"] = style.strip()
            if not any(item == font_file for item in result["files"]):
                result["files"].append(font_file)
    return sorted(grouped.values(), key=lambda item: item["family"].casefold())


def _extract_families(payload: Any) -> list[str]:
    """Keep the original family-only helper for callers and old fixtures."""
    return [entry["family"] for entry in _extract_fonts(payload)]


def _enumerate() -> list[FontFamily]:
    try:
        completed = subprocess.run(
            ["system_profiler", "SPFontsDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout:
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    return _extract_fonts(payload)


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
    computed = _normalise(_enumerate())
    file_map: dict[str, Path] = {}
    for entry in computed:
        for font_file in entry["files"]:
            font_id = font_file.get("id")
            if not isinstance(font_id, str):
                continue
            # Re-resolve every enumerated path before adding it to the map.
            # The map is populated from server-side data, never client input.
            resolved = _allowed_font_path(_EXTRACTED_PATHS.get(font_id, Path("")))
            if resolved is not None:
                file_map[font_id] = resolved
    with _LOCK:
        if _CACHE is None:
            _CACHE = computed
            _FILE_MAP = file_map
        return _CACHE


def installed_families() -> list[str]:
    return [entry["family"] for entry in installed_fonts()]


def font_path(font_id: str) -> Path | None:
    installed_fonts()
    with _LOCK:
        path = _FILE_MAP.get(font_id)
    if path is None:
        return None
    return _allowed_font_path(path)


def reset_cache_for_tests() -> None:
    global _CACHE, _FILE_MAP
    with _LOCK:
        _CACHE = None
        _FILE_MAP = {}
        _EXTRACTED_PATHS.clear()
