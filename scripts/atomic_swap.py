"""Atomically replace a completed native app bundle when possible."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

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


def atomic_replace(staged: Path, live: Path) -> None:
    """Put *staged* at *live*, retaining the old bundle on any failed swap."""

    if not staged.is_dir():
        raise FileNotFoundError(f"missing staged bundle: {staged}")
    live.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not live.exists() and not live.is_symlink():
        os.rename(staged, live)
        return

    if _rename_swap(staged, live):
        return

    # Non-macOS fallback for tests and development environments. The old
    # bundle is restored if the second rename fails; macOS uses the true
    # single-operation exchange above.
    backup = live.with_name(f".{live.name}.previous.{os.getpid()}")
    os.rename(live, backup)
    try:
        os.rename(staged, live)
    except Exception:
        os.rename(backup, live)
        raise
    shutil.rmtree(backup)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Atomically replace a native app bundle")
    parser.add_argument("staged", type=Path)
    parser.add_argument("live", type=Path)
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
            atomic_replace(args.staged, args.live)
    except NativeRuntimeLockError as exc:
        print(
            "REFUSING native bundle swap: "
            f"{exc}. The live bundle was not touched.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
