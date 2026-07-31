"""Shared macOS trust checks for the signed Wiki native bundle."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


TAURI_BUNDLE_IDENTIFIER = "com.hwang2409.wiki"
_TEAM_IDENTIFIER = re.compile(r"^[A-Z0-9]{10}$")


def designated_requirement(team_identifier: str) -> str:
    """Build the one Apple-anchor requirement used by install and runtime."""

    if _TEAM_IDENTIFIER.fullmatch(team_identifier) is None:
        raise ValueError("invalid Apple team identifier")
    return (
        f'anchor apple generic and identifier "{TAURI_BUNDLE_IDENTIFIER}" '
        f'and certificate leaf[subject.OU] = "{team_identifier}"'
    )


def verify_designated_requirement(
    executable: Path,
    team_identifier: str,
    *,
    deep: bool = False,
) -> bool:
    """Evaluate the shared designated requirement with codesign."""

    try:
        requirement = designated_requirement(team_identifier)
    except ValueError:
        return False
    arguments = ["/usr/bin/codesign", "--verify"]
    if deep:
        arguments.append("--deep")
    arguments.extend(
        [
            "--strict",
            "--test-requirement",
            f"={requirement}",
            str(executable),
        ]
    )
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0
