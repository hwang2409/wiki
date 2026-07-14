---
type: til
tags: [agents, verification]
created: 2026-07-08
updated: 2026-07-14
---

# Instance-pinning verification


## The shape

Multiple live instances of one system coexist; a fix or test silently validates instance A while the consumer runs instance B. Four observed failures:

- Bazel MCP runs the MAIN checkout — a worker's "passes locally" validated the wrong branch (PHO-13215 shard failure).
- `worktree_bazel/run_bazel_test` without `worktree_path` ran its server's
  `pho-13669` checkout during PHO-13690; pass the exact current worktree path.
- The primary phoebe checkout sits on a stale branch — explore agents reported wrong repo facts twice; fix: `git fetch` + read via `git show <pinned-sha>:<path>`.
- Wiki web backend (:8011) hot-reloads; the native Wiki.app sidecar is a FROZEN PyInstaller build — a resolver fix verified by curl was invisible in the app until `make native-build` + relaunch.

## The rule

Before declaring anything verified, name the instance you tested AND prove it is the instance the consumer uses: branch/SHA for code, port/PID for services, checkout path for tools. Codified in the tmux-ticket skills (worker prompts) as the Instance-pinning rule.

## Remaining structural gap

Native Wiki.app staleness is still only human-detectable — durable fix is a build-SHA staleness banner + dev-preference for a live backend (proposed to wiki-dev, unticketed as of 2026-07-08).
