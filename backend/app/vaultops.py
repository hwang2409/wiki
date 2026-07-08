"""Shared vault file operations — used by the FastAPI backend and the `wiki` CLI.

Smart rename rewrites wikilinks vault-wide (code-aware) and updates map.md in
the same action. Hard delete prunes the map line; git is the undo.
"""

from __future__ import annotations

import re
from pathlib import Path

FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_SPLIT_RE = re.compile(r"(`[^`\n]*`)")
SAFE_SEGMENT_RE = re.compile(r"^[^.\s/][^/]*$")


class VaultOpError(ValueError):
    pass


def _validate_rel(vault: Path, rel: str) -> Path:
    rel = rel.strip().removeprefix("./")
    if not rel.endswith(".md"):
        raise VaultOpError(f"not a markdown path: {rel}")
    parts = Path(rel).parts
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise VaultOpError(f"unsafe path: {rel}")
    target = (vault / rel).resolve()
    try:
        target.relative_to(vault.resolve())
    except ValueError as exc:
        raise VaultOpError(f"path escapes vault: {rel}") from exc
    return target


def rewrite_outside_code(text: str, transform) -> str:
    """Apply `transform` to prose only — fenced blocks and inline code pass through."""
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = INLINE_CODE_SPLIT_RE.split(line)
        out.append(
            "".join(part if part.startswith("`") else transform(part) for part in parts)
        )
    return "\n".join(out)


def _iter_notes(vault: Path):
    for path in sorted(vault.rglob("*.md")):
        if ".obsidian" in path.parts:
            continue
        yield path


def _prune_empty_dirs(vault: Path, start: Path) -> None:
    current = start
    vault = vault.resolve()
    while current.resolve() != vault and current.is_dir():
        if any(current.iterdir()):
            break
        current.rmdir()
        current = current.parent


def rename_note(vault: Path, old_rel: str, new_rel: str) -> list[str]:
    """Move a note; if the slug changed, rewrite [[wikilinks]] vault-wide.

    Returns vault-relative paths of every file changed (including the moved one).
    """
    old_path = _validate_rel(vault, old_rel)
    new_path = _validate_rel(vault, new_rel)
    if not old_path.is_file():
        raise VaultOpError(f"{old_rel} does not exist")
    if new_path.exists():
        raise VaultOpError(f"{new_rel} already exists")

    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    _prune_empty_dirs(vault, old_path.parent)

    changed = [new_path.relative_to(vault).as_posix()]

    old_slug = old_path.stem
    new_slug = new_path.stem
    if old_slug == new_slug:
        return changed

    link_re = re.compile(r"\[\[" + re.escape(old_slug) + r"(\]\]|\|)")

    def transform(segment: str) -> str:
        return link_re.sub(lambda m: f"[[{new_slug}{m.group(1)}", segment)

    for note in _iter_notes(vault):
        text = note.read_text(encoding="utf-8")
        rewritten = rewrite_outside_code(text, transform)
        if rewritten != text:
            note.write_text(rewritten, encoding="utf-8")
            rel = note.relative_to(vault).as_posix()
            if rel not in changed:
                changed.append(rel)

    return changed


def delete_note(vault: Path, rel: str) -> list[str]:
    """Hard-delete a note; drop its map.md bullet if present. Git is the undo.

    Returns vault-relative paths of changed files (the deleted one first).
    """
    path = _validate_rel(vault, rel)
    if not path.is_file():
        raise VaultOpError(f"{rel} does not exist")

    slug = path.stem
    path.unlink()
    _prune_empty_dirs(vault, path.parent)
    changed = [Path(rel).as_posix()]

    map_path = vault / "map.md"
    if map_path.is_file():
        lines = map_path.read_text(encoding="utf-8").split("\n")
        kept = [
            line
            for line in lines
            if not (line.lstrip().startswith("-") and f"[[{slug}]]" in line)
        ]
        if kept != lines:
            map_path.write_text("\n".join(kept), encoding="utf-8")
            changed.append("map.md")

    return changed
