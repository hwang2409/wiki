"""Shared, zero-dependency path-walking helpers.

Both the FastAPI vault-serving endpoints (``main``) and the standalone
``wiki_artifacts`` MCP server need to open a file that has been validated
as living under an allowed root, without letting a symlink swapped into
any path component (leaf or intermediate directory) escape containment.

The helpers here open each component through ``dir_fd`` with
``O_NOFOLLOW`` so a symlink at any level fails the open. The caller is
responsible for opening the root directory once and passing its fd in.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


def open_root_directory(path: Path | str) -> int:
    """Open an anchor directory with O_NOFOLLOW so a symlinked root fails.

    ``open_relative_file``/``open_relative_directory`` apply O_NOFOLLOW to
    every component under the anchor, but the anchor itself is trusted.
    That is not always safe: a symlink swapped in for ``runs_dir`` would
    silently redirect every subsequent open. Callers of that pattern
    should open the anchor through this helper.

    It canonicalizes the PARENT chain only (so macOS ``/tmp`` and other
    benign parent symlinks still work) and keeps the leaf unresolved,
    then opens it with O_NOFOLLOW | O_DIRECTORY. If the anchor itself
    is a symlink, ``os.open`` raises with ELOOP; if it is not a
    directory, this helper raises ``OSError`` after inspecting the
    ``fstat`` mode. Callers handle either failure.
    """

    absolute = Path(path).absolute()
    resolved = absolute.parent.resolve(strict=False) / absolute.name
    fd = os.open(
        resolved,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"root is not a directory: {resolved}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_relative_file(
    root_fd: int,
    relative_parts: tuple[str, ...],
    *,
    extra_final_flags: int = 0,
) -> int:
    """Open ``relative_parts`` under ``root_fd`` with O_NOFOLLOW per component.

    Intermediate components are opened O_DIRECTORY so a file or symlink
    swapped in for a directory fails immediately. The final component is
    opened with ``extra_final_flags`` OR'd into the read flags — callers
    that need ``O_NONBLOCK`` (to keep FIFOs/devices from blocking the
    open) pass it here.
    """
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    current_fd = os.dup(root_fd)
    try:
        for index, component in enumerate(relative_parts):
            is_final = index == len(relative_parts) - 1
            flags = os.O_RDONLY | no_follow
            if is_final:
                flags |= extra_final_flags
            else:
                flags |= directory_flag
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_relative_directory(root_fd: int, relative_parts: tuple[str, ...]) -> int:
    """Open a directory nested under ``root_fd`` with O_NOFOLLOW per component."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    current_fd = os.dup(root_fd)
    try:
        for component in relative_parts:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory_flag | no_follow,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise
