"""Build a small, deterministic context prelude for a worker kickoff."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import knowledge, workgraph
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
TICKET_RE = re.compile(r"^[A-Z0-9-]+$")
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
        raise PreludeError("ticket must contain only uppercase letters, numbers, and dashes")
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
    marker = f"\n[{marker}]"
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


def _direct_vault_search(vault_dir: Path, terms: Iterable[str]) -> list[dict[str, str]]:
    wanted = tuple(dict.fromkeys(term.casefold() for term in terms if term))
    if not wanted:
        return []
    rows: list[tuple[int, str, str]] = []
    scanned = 0
    for path in sorted(vault_dir.rglob("*.md")):
        if scanned >= 600 or path.is_symlink() or ".obsidian" in path.parts:
            continue
        scanned += 1
        try:
            relative = path.relative_to(vault_dir).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError, ValueError):
            continue
        folded = text.casefold()
        score = sum(folded.count(term) for term in wanted)
        if score <= 0:
            continue
        first = min((folded.find(term) for term in wanted if folded.find(term) >= 0), default=0)
        snippet = text[max(0, first - 80) : first + 280]
        rows.append((score, relative, _clean(snippet, 340)))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [{"path": path, "snippet": snippet} for _score, path, snippet in rows[:MAX_VAULT_RESULTS]]


def _vault_source(
    *,
    vault_dir: Path,
    runtime_dir: Path | None,
    archive_dir: Path | None,
    terms: list[str],
) -> SourceResult:
    if not terms:
        return SourceResult("[no ticket or title keywords]", "empty")
    try:
        index = knowledge.KnowledgeIndex.from_env(
            runtime_dir=runtime_dir,
            archive_dir=archive_dir,
            vault_dir=vault_dir,
        )
        rows: dict[str, dict[str, str]] = {}
        for query in terms[:6]:
            payload = index.search(query, kind="note", limit=MAX_VAULT_RESULTS)
            for row in payload.get("results", []):
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    continue
                rows.setdefault(
                    row["path"],
                    {
                        "path": row["path"],
                        "snippet": _clean(row.get("snippet"), 340),
                    },
                )
        results = list(rows.values())[:MAX_VAULT_RESULTS]
    except Exception as exc:
        warning = _clean(exc, 180)
        try:
            results = _direct_vault_search(vault_dir, terms)
        except Exception as fallback_exc:
            return SourceResult(
                f"[vault search unavailable: {warning}; fallback failed: {_clean(fallback_exc, 120)}]",
                "failed",
                warning,
            )
        if not results:
            return SourceResult(
                f"[vault search unavailable: {warning}; no fallback matches]",
                "degraded",
                warning,
            )
        body = "\n".join(f"- {row['path']}: {row['snippet']}" for row in results)
        return SourceResult(body, "degraded", warning, len(results))
    if not results:
        results = _direct_vault_search(vault_dir, terms)
    if not results:
        return SourceResult("[no matching vault notes]", "empty")
    body = "\n".join(f"- {row['path']}: {row['snippet']}" for row in results)
    return SourceResult(body, "ok", item_count=len(results))


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


def _changed_files(repo_root: Path) -> set[str]:
    for base in ("origin/main", "main"):
        try:
            output = _run_git(repo_root, ["diff", "--name-only", f"{base}...HEAD", "--"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
        files = {line.strip() for line in output.splitlines() if line.strip()}
        if files:
            return files
    try:
        return {
            line.strip()
            for line in _run_git(repo_root, ["diff", "--name-only", "HEAD", "--"]).splitlines()
            if line.strip()
        }
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return set()


def _recent_pr_source(repo_root: Path) -> SourceResult:
    try:
        current_files = _changed_files(repo_root)
        log = _run_git(
            repo_root,
            [
                "log",
                "--all",
                "--first-parent",
                "-m",
                "--no-decorate",
                "--name-only",
                "--format=%H%x00%s",
                "-n",
                str(MAX_RECENT_COMMITS),
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
    lines = [
        f"- ticket: {graph_ticket}",
        f"- orchestrator: {_clean(graph.get('orch'), 100) or 'unknown'}",
        f"- nodes: {', '.join(node_rows[:12]) or 'none'}",
        f"- edges: {', '.join(f'{kind}={count}' for kind, count in sorted(edge_kinds.items())) or 'none'}",
    ]
    return SourceResult("\n".join(lines), "ok", item_count=len(node_rows) + len(edge_kinds))


def _source_section(name: str, result: SourceResult) -> tuple[str, bool]:
    header = f"## {name} [source budget: {SOURCE_BUDGETS[name]} chars]"
    section, truncated = _fit(
        f"{header}\n{result.body}",
        SOURCE_BUDGETS[name],
        f"truncated: {name} source budget reached",
    )
    return section, truncated


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
        self.repo_root = _safe_root(repo_root, "repository")
        self.vault_dir = _safe_root(vault_dir, "vault")
        try:
            self.status_dir = Path(status_dir).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PreludeError("workgraph status is not a readable path") from exc
        self.runtime_dir = _optional_root(runtime_dir)
        self.archive_dir = _optional_root(archive_dir)

    def build(self, *, ticket: str, title: str = "", prompt: str = "") -> PreludeResult:
        normalized_ticket = _validate_ticket(ticket)
        terms = _keywords(normalized_ticket, title, prompt)
        source_builders: list[tuple[str, Callable[[], SourceResult]]] = [
            (
                "vault notes",
                lambda: _vault_source(
                    vault_dir=self.vault_dir,
                    runtime_dir=self.runtime_dir,
                    archive_dir=self.archive_dir,
                    terms=terms,
                ),
            ),
            ("recent merged prs", lambda: _recent_pr_source(self.repo_root)),
            (
                "related workgraph",
                lambda: _workgraph_source(normalized_ticket, self.status_dir),
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

    context = prelude.strip()
    goal = prompt.strip()
    if not context:
        return prompt
    if not goal:
        return context
    return f"{context}\n\n# kickoff prompt\n{goal}"


def bound_override(value: str) -> str:
    """Bound an edited preview before it is added to a worker prompt."""

    bounded, _truncated = _fit(
        value.strip(),
        MAX_PRELUDE_CHARS,
        f"truncated: overall prelude budget of {MAX_PRELUDE_CHARS} chars reached",
    )
    return bounded
