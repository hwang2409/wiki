"""Atomically replace a completed native app bundle when possible."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.native_lifecycle import (
    NativeRuntimeLockError,
    hold_runtime_locks,
)


def _rename_swap(first: Path, second: Path) -> bool:
    """Exchange two paths using macOS's atomic rename extension."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        return False
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    if renameatx_np(
        -2,
        os.fsencode(first),
        -2,
        os.fsencode(second),
        0x00000002,  # RENAME_SWAP
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(first), str(second))
    return True


@contextmanager
def _block_swap_signals():
    """Keep termination pending until exchange and completion marker both exist."""

    blocked = {signal.SIGINT, signal.SIGTERM}
    if hasattr(signal, "SIGHUP"):
        blocked.add(signal.SIGHUP)
    if hasattr(signal, "SIGQUIT"):
        blocked.add(signal.SIGQUIT)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _identity(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino]


def _write_intent(intent: Path, staged: Path, live: Path) -> None:
    intent.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    payload = {
        "staged": str(staged.resolve()),
        "live": str(live.resolve()),
        "staged_identity": _identity(staged),
        "live_identity": _identity(live),
    }
    temporary = intent.with_name(f".{intent.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, intent)


def _read_intent(intent: Path) -> dict[str, Any]:
    with intent.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid swap intent: {intent}")
    return payload


def _already_swapped(intent: Path, staged: Path, live: Path) -> bool:
    payload = _read_intent(intent)
    if payload.get("staged") != str(staged.resolve()) or payload.get(
        "live"
    ) != str(live.resolve()):
        raise RuntimeError(f"swap intent belongs to a different bundle: {intent}")
    before_staged = payload.get("staged_identity")
    before_live = payload.get("live_identity")
    if _identity(live) == before_staged and _identity(staged) == before_live:
        return True
    if _identity(staged) != before_staged or _identity(live) != before_live:
        raise RuntimeError(
            "swap intent does not match the current bundles; refusing an unsafe retry"
        )
    return False


def atomic_replace(
    staged: Path,
    live: Path,
    success_sentinel: Path | None = None,
    swap_intent: Path | None = None,
) -> bool:
    """Exchange bundles once, returning false when a prior exchange is detected."""

    intent_exists = swap_intent is not None and swap_intent.exists()
    if intent_exists:
        assert swap_intent is not None
        if _already_swapped(swap_intent, staged, live):
            if success_sentinel is not None:
                success_sentinel.touch()
            return False

    if not staged.is_dir():
        raise FileNotFoundError(f"missing staged bundle: {staged}")
    live.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if swap_intent is not None and not intent_exists:
        _write_intent(swap_intent, staged, live)

    with _block_swap_signals():
        if not live.exists() and not live.is_symlink():
            os.rename(staged, live)
        elif not _rename_swap(staged, live):
            # Non-macOS fallback for tests and development environments. The
            # old bundle is restored if the second rename fails; macOS uses
            # the true single-operation exchange above.
            backup = live.with_name(f".{live.name}.previous.{os.getpid()}")
            os.rename(live, backup)
            try:
                os.rename(staged, live)
            except Exception:
                os.rename(backup, live)
                raise
            shutil.rmtree(backup)
        if success_sentinel is not None:
            success_sentinel.touch()
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Atomically replace a native app bundle")
    parser.add_argument("staged", type=Path)
    parser.add_argument("live", type=Path)
    parser.add_argument(
        "--success-sentinel",
        type=Path,
        help="write this marker immediately after the exchange succeeds",
    )
    parser.add_argument(
        "--swap-intent",
        type=Path,
        help="record bundle identities so an interrupted swap cannot be repeated",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(
            os.environ.get("WIKI_AGENT_RUNTIME_DIR")
            or Path.home() / ".wiki" / "agent-runtime"
        ).expanduser(),
    )
    parser.add_argument(
        "--allow-missing-app-lock",
        action="store_true",
        default=os.environ.get("WIKI_NATIVE_ALLOW_MISSING_APP_LOCK", "").lower()
        in {"1", "true", "yes", "on"},
    )
    args = parser.parse_args()
    try:
        with hold_runtime_locks(
            args.runtime_dir,
            allow_missing_app_lock=args.allow_missing_app_lock,
        ):
            already_swapped = not atomic_replace(
                args.staged,
                args.live,
                args.success_sentinel,
                args.swap_intent,
            )
            if already_swapped:
                print("swap already applied; refusing to exchange the bundles again")
    except NativeRuntimeLockError as exc:
        print(
            "REFUSING native bundle swap: "
            f"{exc}. The live bundle was not touched.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
