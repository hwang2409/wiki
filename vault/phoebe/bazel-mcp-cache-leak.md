---
type: til
tags: [phoebe]
created: 2026-09-14
updated: 2026-09-14
---

# Bazel-MCP bootstrap cache leak (387G)

Discovered 2026-09-14 while diagnosing a 97%-full disk (874G/926G used, 30G free).

`~/.cache/phoebe/worktree-bazel-mcp/` held **387G** across **~30,700 entries**: only 3 live
`py3.x-<hash>` venv dirs (~50M each) plus tens of thousands of leaked hidden
`.build-py3.x-<hash>.XXXXXX` temp dirs (~13M avg), dated Aug 5 – Sep 14. The phoebe
worktree bazel-mcp bootstrap creates a temp build dir per invocation and fails to remove it
when the build is interrupted or loses the bootstrap-lock race — every agent/worktree
session leaks one.

Related bulk in the same diagnosis (all regenerable caches):

- `~/.cache/bazel-disk` 49G + `~/.cache/phoebe-bazel-disk-cache` 48G — two parallel bazel disk caches
- `~/.cache/phoebe/python-venvs` 23G
- `~/.cache/uv` 14G

Gotcha: `du -sh ~/.cache/phoebe/worktree-bazel-mcp/*` shows ~150M because the leaked dirs
are dot-prefixed — glob misses them. Use `ls -la | wc -l` or `du -sh` on the parent.

Fix direction: the leaked `.build-*` dirs are safe to delete (temp build staging, all stale).
Real fix belongs in the phoebe bazel-mcp bootstrap script: cleanup trap / stale-dir sweep on
startup.
