from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


APP_LOCK_NAME = "app.lock"
SUPERVISOR_LOCK_NAME = "supervisor.lock"


class NativeRuntimeLockError(RuntimeError):
    pass


def _acquire_lock(path: Path, label: str) -> BinaryIO:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        handle = path.open("a+b")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise NativeRuntimeLockError(f"cannot open {label} lock {path}: {exc}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        if isinstance(exc, BlockingIOError):
            raise NativeRuntimeLockError(f"{label} lock is held: {path}") from exc
        raise NativeRuntimeLockError(f"cannot acquire {label} lock {path}: {exc}") from exc
    return handle


def _release_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def hold_app_lock(
    runtime_dir: Path | str,
    *,
    allow_missing_app_lock: bool = False,
) -> Iterator[BinaryIO]:
    """Model the GUI-owned app-lifetime lock in tests and native helpers."""

    path = Path(runtime_dir).expanduser() / APP_LOCK_NAME
    if allow_missing_app_lock and not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600)
    handle = _acquire_lock(path, "Wiki app")
    try:
        yield handle
    finally:
        _release_lock(handle)


@contextmanager
def hold_supervisor_lock(runtime_dir: Path | str) -> Iterator[BinaryIO]:
    """Hold the supervisor lock after its owner has stopped."""

    path = Path(runtime_dir).expanduser() / SUPERVISOR_LOCK_NAME
    handle = _acquire_lock(path, "supervisor")
    try:
        yield handle
    finally:
        _release_lock(handle)


@contextmanager
def hold_runtime_locks(
    runtime_dir: Path | str,
    *,
    allow_missing_app_lock: bool = False,
) -> Iterator[tuple[BinaryIO, BinaryIO]]:
    """Hold app and supervisor locks across a guarded bundle replacement.

    The app lock is deliberately required once deployed. A missing app lock
    means an older sidecar may be running and cannot be distinguished from a
    stopped app; the one-time deployment override may create and hold it only
    after the supervisor lock is also proven available.
    """

    runtime = Path(runtime_dir).expanduser()
    app_path = runtime / APP_LOCK_NAME
    if not app_path.exists() and not allow_missing_app_lock:
        raise NativeRuntimeLockError(
            f"app lock is missing: {app_path}; the installed sidecar may be old. "
            "Quit Wiki.app and pass ALLOW_MISSING_APP_LOCK=1 for the one-time upgrade."
        )

    with hold_app_lock(
        runtime,
        allow_missing_app_lock=allow_missing_app_lock,
    ) as app_handle:
        with hold_supervisor_lock(runtime) as supervisor_handle:
            yield app_handle, supervisor_handle
