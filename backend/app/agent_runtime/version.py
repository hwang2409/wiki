from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def _runtime_fingerprint() -> str:
    """Identify the supervisor code loaded by this process."""

    digest = hashlib.sha256()
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        stat = executable.stat()
        digest.update(
            f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}".encode()
        )
    else:
        runtime_dir = Path(__file__).resolve().parent
        sources = [*runtime_dir.glob("*.py"), runtime_dir.parent / "wiki_artifacts.py"]
        for path in sorted(sources):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# Capture this at import time. A detached frozen daemon can keep executing an
# old inode after Wiki.app replaces the bundle at the same filesystem path.
RUNTIME_FINGERPRINT = _runtime_fingerprint()
