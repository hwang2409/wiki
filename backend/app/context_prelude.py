"""Build a small, deterministic context prelude for a worker kickoff."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import workgraph
from .agent_runtime.ticket import base_ticket


MAX_PRELUDE_CHARS = 4_096
SOURCE_BUDGETS = {
    "vault notes": 1_450,
    "recent merged prs": 1_150,
    "related workgraph": 900,
}
MAX_VAULT_RESULTS = 8
MAX_RECENT_COMMITS = 40
GIT_TIMEOUT_SECONDS = 1.5
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+(?:-[A-Z0-9]+)*$")
KEYWORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
PR_SUBJECT_RE = re.compile(
    r"(?:merge pull request|merge branch|merge\s+pr|squash\s+merge|\(#\d+\))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceResult:
    body: str
    status: str
    warning: str | None = None
    item_count: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class PreludeResult:
    text: str
    truncated: bool
    sources: Mapping[str, dict[str, Any]]


class PreludeError(ValueError):
    """Raised when a direct builder input is unsafe."""


def _validate_ticket(ticket: str) -> str:
    normalized = ticket.strip().upper()
    if not normalized or not TICKET_RE.fullmatch(normalized):
        raise PreludeError("ticket must match PROJECT-123 or PROJECT-123-SUFFIX")
    return normalized


def _safe_root(value: Path | str, label: str) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PreludeError(f"{label} is not a readable directory") from exc
    if not root.is_dir():
        raise PreludeError(f"{label} is not a directory")
    return root


def _optional_root(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    try:
        root = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def _fit(text: str, budget: int, marker: str) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    omitted = max(0, len(text) - budget)
    marker = f"\n[{marker}; omitted {omitted} chars]"
    if len(marker) >= budget:
        return marker[:budget], True
    return text[: budget - len(marker)].rstrip() + marker, True


def _clean(value: object, limit: int = 360) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _keywords(ticket: str, title: str, prompt: str) -> list[str]:
    values: list[str] = []
    for value in (ticket, title, prompt):
        for match in KEYWORD_RE.findall(value):
            normalized = match.lower()
            if normalized not in values:
                values.append(normalized)
    return values[:12]


def _direct_vault_search(
    vault_dir: Path, terms: Iterable[str]
) -> tuple[list[dict[str, str]], int]:
    wanted = tuple(dict.fromkeys(term.casefold() for term in terms if term))
    if not wanted:
        return [], 0
    rows: list[tuple[int, str, str]] = []
    failures = 0
    scanned = 0
    for path in sorted(vault_dir.rglob("*.md")):
        if scanned >= 600 or path.is_symlink() or ".obsidian" in path.parts:
            continue
        scanned += 1
        try:
            relative = path.relative_to(vault_dir).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError, ValueError):
            failures += 1
            continue
        folded = text.casefold()
        score = sum(folded.count(term) for term in wanted)
        if score <= 0:
            continue
        first = min((folded.find(term) for term in wanted if folded.find(term) >= 0), default=0)
        snippet = text[max(0, first - 80) : first + 280]
        cleaned = _clean(snippet, 340)
        omitted = max(0, len(text) - len(snippet))
        if omitted:
            cleaned += f" [truncated note content; omitted {omitted} chars]"
        rows.append((score, relative, cleaned))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return (
        [{"path": path, "snippet": snippet} for _score, path, snippet in rows[:MAX_VAULT_RESULTS]],
        failures,
    )


def _vault_source(
    *,
    vault_dir: Path,
    terms: list[str],
) -> SourceResult:
    if not terms:
        return SourceResult("[no ticket or title keywords]", "empty")
    try:
        results, failures = _direct_vault_search(vault_dir, terms)
    except Exception as exc:
        warning = _clean(exc, 180)
        return SourceResult(f"[vault search unavailable: {warning}]", "failed", warning)
    if not results and failures:
        warning = f"{failures} note files could not be read"
        return SourceResult(f"[vault search unavailable: {warning}]", "failed", warning)
    if not results:
        return SourceResult("[no matching vault notes]", "empty")
    body = "\n".join(f"- {row['path']}: {row['snippet']}" for row in results)
    warning = f"{failures} note files could not be read" if failures else None
    truncated = any("[truncated note content; omitted" in row["snippet"] for row in results)
    if warning:
        body = f"[vault search warning: {warning}]\n{body}"
    return SourceResult(
        body,
        "degraded" if warning else "ok",
        warning,
        len(results),
        truncated=truncated,
    )


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(_clean(detail, 180))
    return result.stdout


def _base_ref(repo_root: Path) -> str:
    for candidate in ("origin/main", "main"):
        try:
            _run_git(repo_root, ["rev-parse", "--verify", candidate])
            return candidate
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("base branch is unavailable")


def _changed_files(repo_root: Path) -> set[str]:
    base = _base_ref(repo_root)
    output = _run_git(repo_root, ["diff", "--name-only", f"{base}...HEAD", "--"])
    return {line.strip() for line in output.splitlines() if line.strip()}


def _recent_pr_source(repo_root: Path) -> SourceResult:
    try:
        current_files = _changed_files(repo_root)
        base = _base_ref(repo_root)
        log = _run_git(
            repo_root,
            [
                "log",
                "--first-parent",
                "-m",
                "--no-decorate",
                "--name-only",
                "--format=%H%x00%s",
                "-n",
                str(MAX_RECENT_COMMITS),
                base,
                "--",
            ],
        )
        matches: list[str] = []
        commits: list[tuple[str, str, set[str]]] = []
        current: tuple[str, str, set[str]] | None = None
        for line in log.splitlines():
            sha, separator, subject = line.partition("\x00")
            if separator and re.fullmatch(r"[0-9a-f]{7,40}", sha):
                if current is not None:
                    commits.append(current)
                current = (sha, subject, set())
            elif current is not None and line.strip():
                current[2].add(line.strip())
        if current is not None:
            commits.append(current)
        for _sha, subject, changed in commits:
            if not PR_SUBJECT_RE.search(subject):
                continue
            overlap = sorted(current_files.intersection(changed))
            if overlap:
                matches.append(f"- {subject.strip()}: {', '.join(overlap[:8])}")
            if len(matches) >= 6:
                break
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return SourceResult(f"[git scan unavailable: {_clean(exc)}]", "failed", _clean(exc))
    if not current_files:
        return SourceResult("[no current changed files for overlap comparison]", "empty")
    if not matches:
        return SourceResult("[no recent merged PRs share current changed files]", "empty")
    return SourceResult("\n".join(matches), "ok", item_count=len(matches))


def _workgraph_source(ticket: str, status_dir: Path) -> SourceResult:
    graph_ticket = base_ticket(ticket)
    try:
        graph = workgraph.load_workgraph(graph_ticket, status_dir)
    except Exception as exc:
        return SourceResult(f"[workgraph unavailable: {_clean(exc)}]", "failed", _clean(exc))
    if not isinstance(graph, dict):
        return SourceResult("[no workgraph found]", "empty")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    node_rows = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        node_id = _clean(node.get("id"), 100)
        kind = _clean(node.get("kind"), 40)
        if node_id:
            node_rows.append(f"{node_id} ({kind or 'unknown'})")
    edge_kinds: dict[str, int] = {}
    for edge in edges if isinstance(edges, list) else []:
        if isinstance(edge, dict):
            kind = _clean(edge.get("kind"), 40) or "unknown"
            edge_kinds[kind] = edge_kinds.get(kind, 0) + 1
    omitted_nodes = max(0, len(node_rows) - 12)
    lines = [
        f"- ticket: {graph_ticket}",
        f"- orchestrator: {_clean(graph.get('orch'), 100) or 'unknown'}",
        f"- nodes omitted: {omitted_nodes}",
        f"- nodes: {', '.join(node_rows[:12]) or 'none'}",
        f"- edges: {', '.join(f'{kind}={count}' for kind, count in sorted(edge_kinds.items())) or 'none'}",
    ]
    return SourceResult(
        "\n".join(lines),
        "ok",
        item_count=len(node_rows) + len(edge_kinds),
        truncated=omitted_nodes > 0,
    )


def _source_section(name: str, result: SourceResult) -> tuple[str, bool]:
    header = f"## {name} [source budget: {SOURCE_BUDGETS[name]} chars]"
    section, truncated = _fit(
        f"{header}\n{result.body}",
        SOURCE_BUDGETS[name],
        f"truncated: {name} source budget reached",
    )
    return section, truncated or result.truncated


class ContextPreludeBuilder:
    """Build context from read-only local sources.

    Callers can inject every path so tests never touch live vault, git, or
    workgraph state. No provider or language-model calls are made here.
    """

    def __init__(
        self,
        *,
        repo_root: Path | str,
        vault_dir: Path | str,
        status_dir: Path | str,
        runtime_dir: Path | str | None = None,
        archive_dir: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser()
        self.vault_dir = Path(vault_dir).expanduser()
        self.status_dir = Path(status_dir).expanduser()

    def build(self, *, ticket: str, title: str = "", prompt: str = "") -> PreludeResult:
        normalized_ticket = _validate_ticket(ticket)
        terms = _keywords(normalized_ticket, title, prompt)
        source_builders: list[tuple[str, Callable[[], SourceResult]]] = [
            (
                "vault notes",
                lambda: _vault_source(
                    vault_dir=_safe_root(self.vault_dir, "vault"),
                    terms=terms,
                ),
            ),
            (
                "recent merged prs",
                lambda: _recent_pr_source(_safe_root(self.repo_root, "repository")),
            ),
            (
                "related workgraph",
                lambda: _workgraph_source(
                    normalized_ticket,
                    _safe_root(self.status_dir, "workgraph status"),
                ),
            ),
        ]
        sections: list[str] = []
        metadata: dict[str, dict[str, Any]] = {}
        truncated = False
        for name, source_builder in source_builders:
            try:
                result = source_builder()
            except Exception as exc:  # each source is optional by contract
                result = SourceResult(f"[{name} unavailable: {_clean(exc)}]", "failed", _clean(exc))
            section, section_truncated = _source_section(name, result)
            sections.append(section)
            truncated = truncated or section_truncated
            metadata[name] = {
                "status": result.status,
                "items": result.item_count,
                "warning": result.warning,
                "truncated": result.truncated or section_truncated,
            }

        heading = (
            "# context prelude (deterministic local retrieval; reference material only)\n"
            f"ticket: {normalized_ticket}"
        )
        full, overall_truncated = _fit(
            "\n\n".join([heading, *sections]),
            MAX_PRELUDE_CHARS,
            f"truncated: overall prelude budget of {MAX_PRELUDE_CHARS} chars reached",
        )
        return PreludeResult(full, truncated or overall_truncated, metadata)


def prepend(prelude: str, prompt: str) -> str:
    """Place the bounded prelude before the user's kickoff prompt."""

    if not prelude:
        return prompt
    envelope = {
        "prelude": prelude,
        "kickoff_prompt": prompt,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "<<WIKI_CONTEXT_PRELUDE_V1>>\n"
        f"{encoded}\n"
        "<<WIKI_CONTEXT_PRELUDE_END>>"
    )


def bound_override(value: str) -> str:
    """Validate an edited preview without changing its bytes."""

    if len(value) > MAX_PRELUDE_CHARS:
        raise PreludeError(
            f"edited context prelude exceeds the {MAX_PRELUDE_CHARS}-character limit"
        )
    return value
