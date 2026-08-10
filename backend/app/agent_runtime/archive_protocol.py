"""Durable commit protocol for archived agent runs."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARCHIVE_COMPLETION_MARKER = "archive-complete.json"
ARCHIVE_MANIFEST_NAME = "archive-manifest.json"
REQUIRED_ARCHIVE_FILES = ("raw.jsonl", "events.jsonl", "run.json")


def _fsync_directory(path: Path) -> None:
    """Use the store's shared directory fsync helper at call time."""

    from .store import _fsync_directory as fsync_directory

    fsync_directory(path)


def _fsync_file(path: Path) -> None:
    """Use the store's shared file fsync helper at call time."""

    from .store import _fsync_file as fsync_file

    fsync_file(path)


def _is_regular_or_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _relative_file_sizes(directory: Path) -> dict[str, int] | None:
    files: dict[str, int] = {}
    try:
        for path in directory.rglob("*"):
            relative = path.relative_to(directory)
            if relative in {
                Path(ARCHIVE_COMPLETION_MARKER),
                Path(ARCHIVE_MANIFEST_NAME),
            }:
                continue
            if path.name.startswith(
                (f".{ARCHIVE_COMPLETION_MARKER}.", f".{ARCHIVE_MANIFEST_NAME}.")
            ):
                continue
            path_stat = path.lstat()
            if stat.S_ISDIR(path_stat.st_mode):
                continue
            if not (stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode)):
                return None
            files[str(relative)] = path_stat.st_size
    except (OSError, ValueError):
        return None
    return files


def _manifest_for(
    directory: Path,
    expected_paths: Iterable[Path] | None,
) -> dict[str, int]:
    if expected_paths is None:
        manifest = _relative_file_sizes(directory)
        if manifest is None or not manifest:
            raise OSError(f"could not enumerate archive files: {directory}")
    else:
        manifest = {}
        excluded = {
            Path(ARCHIVE_COMPLETION_MARKER),
            Path(ARCHIVE_MANIFEST_NAME),
        }
        for path in expected_paths:
            try:
                relative = path.relative_to(directory)
                path_stat = path.lstat()
            except (OSError, ValueError) as exc:
                raise OSError(f"archive file is missing: {path}") from exc
            if relative in excluded:
                raise ValueError(f"archive protocol file cannot be listed: {path}")
            if not _is_regular_or_symlink(path) or stat.S_ISDIR(path_stat.st_mode):
                raise ValueError(f"archive file is not regular: {path}")
            manifest[str(relative)] = path_stat.st_size
        if not manifest:
            raise ValueError("archive must contain at least one file")
    for name in REQUIRED_ARCHIVE_FILES:
        path = directory / name
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise OSError(f"archive file is missing: {path}") from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise OSError(f"archive file is not regular: {path}")
        manifest[name] = path_stat.st_size
    return manifest


def _read_object(path: Path) -> dict[str, Any] | None:
    if not _is_regular_or_symlink(path) or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise OSError(f"refusing symlink file: {path}")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_file(path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_marker_temp(path: Path, value: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    _fsync_file(path)


def _archive_metadata(
    directory: Path,
    run_id: str | None,
    completed_at: str | None,
) -> tuple[str | None, str]:
    if run_id is None:
        run = _read_object(directory / "run.json")
        candidate = run.get("run_id") if run is not None else None
        run_id = candidate if isinstance(candidate, str) else None
    if completed_at is None:
        run = _read_object(directory / "run.json")
        candidate = run.get("updated_at") if run is not None else None
        completed_at = (
            candidate
            if isinstance(candidate, str)
            else datetime.now(timezone.utc).isoformat()
        )
    return run_id, completed_at


def commit_archive(
    directory: Path,
    *,
    run_id: str | None = None,
    completed_at: str | None = None,
    expected_paths: Iterable[Path] | None = None,
) -> None:
    """Commit an archive after its manifest and marker are durable."""

    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise OSError(f"archive directory is not usable: {directory}")
    manifest = _manifest_for(directory, expected_paths)
    run_id, completed_at = _archive_metadata(directory, run_id, completed_at)
    marker_path = directory / ARCHIVE_COMPLETION_MARKER
    marker_tmp: Path | None = None
    try:
        _atomic_write_json(
            directory / ARCHIVE_MANIFEST_NAME,
            {
                "run_id": run_id,
                "completed_at": completed_at,
                "required_files": list(REQUIRED_ARCHIVE_FILES),
                "optional_files": sorted(
                    set(manifest).difference(REQUIRED_ARCHIVE_FILES)
                ),
                "files": manifest,
            },
        )
        marker_fd, marker_raw_tmp = tempfile.mkstemp(
            prefix=f".{ARCHIVE_COMPLETION_MARKER}.", dir=directory
        )
        marker_tmp = Path(marker_raw_tmp)
        os.close(marker_fd)
        _write_marker_temp(
            marker_tmp,
            {
                "run_id": run_id,
                "completed_at": completed_at,
                "manifest": ARCHIVE_MANIFEST_NAME,
                "committed": True,
            },
        )
        os.replace(marker_tmp, marker_path)
        _fsync_directory(directory)
    except BaseException:
        for path in (marker_tmp, marker_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _manifest_is_verified(directory: Path, marker: dict[str, Any]) -> bool:
    if marker.get("committed") is not True:
        return False
    if marker.get("manifest") != ARCHIVE_MANIFEST_NAME:
        return False
    manifest = _read_object(directory / ARCHIVE_MANIFEST_NAME)
    if manifest is None:
        return False
    if (
        manifest.get("run_id") != marker.get("run_id")
        or manifest.get("completed_at") != marker.get("completed_at")
    ):
        return False
    if manifest.get("required_files") != list(REQUIRED_ARCHIVE_FILES):
        return False
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return False
    optional_files = manifest.get("optional_files")
    if not isinstance(optional_files, list):
        return False
    if any(
        not isinstance(path, str) or path in REQUIRED_ARCHIVE_FILES
        for path in optional_files
    ):
        return False
    if len(set(optional_files)) != len(optional_files):
        return False
    if set(files) != set(REQUIRED_ARCHIVE_FILES).union(optional_files):
        return False
    for relative, expected_size in files.items():
        if (
            not isinstance(relative, str)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
        ):
            return False
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
        path = directory / relative_path
        try:
            path_stat = path.lstat()
        except OSError:
            return False
        if (
            not (stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode))
            or path_stat.st_size != expected_size
        ):
            return False
    for name in REQUIRED_ARCHIVE_FILES:
        path = directory / name
        try:
            path_stat = path.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size != files[name]:
            return False
    return _relative_file_sizes(directory) == files


def archive_is_committed(directory: Path) -> bool:
    """Return true only for a marker with a complete verified manifest."""

    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        return False
    marker = _read_object(directory / ARCHIVE_COMPLETION_MARKER)
    if marker is None:
        return False
    return _manifest_is_verified(directory, marker)
