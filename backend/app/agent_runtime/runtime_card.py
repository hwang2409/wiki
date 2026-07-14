from __future__ import annotations

import subprocess
from pathlib import Path

from ..backend_runtime import normalize_loopback_url
from .types import RunRecord


MAX_RUNTIME_CARD_BYTES = 4_096
MAX_PROVIDER_PROMPT_BYTES = 100_000
PROTOCOL_NOTE = "~/me/fun/wiki/vault/tools/orchestrator-worker-protocol.md"
VAULT_CONVENTIONS = "~/me/fun/wiki/vault/meta/conventions.md"


def worktree_branch(worktree: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", worktree, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    branch = result.stdout.strip()
    if result.returncode == 0 and branch:
        return branch
    return "detached-or-unknown"


def _backend_line(record: RunRecord) -> str:
    if not record.backend_base_url:
        return "unavailable (spawn caller did not provide a backend URL)"
    return normalize_loopback_url(record.backend_base_url)


def runtime_card(record: RunRecord, *, status_path: Path) -> str:
    backend = _backend_line(record)
    identity = (
        f"orch_id={record.agent_id}"
        if record.role == "orchestrator"
        else f"ticket={record.agent_id} orch={record.orchestrator_id or 'none'}"
    )
    native_surface = (
        "`wiki search`, `wiki gate`, `wiki agent *`; MCP `render_artifact`, "
        "`search_knowledge`, and orchestrator agent-operation tools"
        if record.role == "orchestrator"
        else "`wiki search`, `wiki gate`, `wiki agent status/watch`; MCP "
        "`render_artifact`, `search_knowledge`"
    )
    common = f"""<WIKI_RUNTIME_CARD v=1>
spawned_by=wiki-supervisor run_id={record.run_id}
identity: {identity} role={record.role} kind={record.provider.legacy_kind}
backend: {backend}
worktree: {record.worktree}
branch: {worktree_branch(record.worktree)}
docs: protocol={PROTOCOL_NOTE} vault_conventions={VAULT_CONVENTIONS}
native surface: {native_surface}
skills: follow injected AGENTS.md/SKILL.md instructions and use available skills before improvising
"""
    if record.role == "orchestrator":
        role = f"""ORCHESTRATOR controls (prefer MCP; CLI equivalents shown exactly):
- list/read: `list_agents`, `read_agent`, `read_agent_events`, `read_agent_pr`; `wiki agent status <id>`; `wiki gate <pr> --expect-sha <sha>`
- spawn worker: `spawn_agent` or `wiki agent spawn <ticket> --kind cc|cdx --role plan|implement|review --model <model> [--effort <effort>] --prompt-file <path> --workdir <path> --orch {record.agent_id}`
- steer: `steer_agent` or `wiki agent steer <id> (--message <text>|--file <path>) [--mode now|on-idle]`
- replace: `replace_agent` or `wiki agent replace <id> [--kind cc|cdx] [--model <model>] [--effort <effort>]`
- archive: `archive_agent` or `wiki agent archive <id> --outcome merged|closed|abandoned`
Use request_id idempotency for retried spawn/steer calls. Gate and archive per the protocol; never use tmux/curl as the Wiki control plane.
"""
    else:
        role = f"""WORKER contract: execute only your assigned {record.role} task. You do not have fleet spawn/steer/archive authority.
Status file: {status_path} — atomically rewrite `{{"state":"working|merge-ready|blocked","pr":null|"<url>","step":"<one line>","blocker":null|"<reason>"}}` on every transition and before long operations.
Isolation: this worktree/run directory is writable; live registry/runtime, other runs, ~/.claude, ~/.codex, vault, live app, and tmux are read-only unless the ticket explicitly says otherwise.
"""
    card = common + role + "</WIKI_RUNTIME_CARD>"
    if len(card.encode("utf-8")) > MAX_RUNTIME_CARD_BYTES:
        raise ValueError("Wiki runtime card exceeds its 4KB budget")
    return card


def inject_runtime_card(
    record: RunRecord,
    prompt: str,
    *,
    status_path: Path,
) -> str:
    combined = f"{runtime_card(record, status_path=status_path)}\n\n{prompt}"
    if len(combined.encode("utf-8")) >= MAX_PROVIDER_PROMPT_BYTES:
        raise ValueError("kickoff prompt plus Wiki runtime card must be smaller than 100KB")
    return combined
