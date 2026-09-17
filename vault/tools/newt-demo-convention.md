---
type: reference
tags: [newt, tooling]
created: 2026-08-19
updated: 2026-08-19
---

# newt demo output convention

Decision (Henry 2026-08-19): all newt demo media (mp4, png, gif, any rendered output) goes in `~/me/fun/tooling/newt/demos/`. Not the repo root, not `newt/`, not worker worktrees.

- Files stay UNTRACKED — never commit media to the repo (repo rule predates this note).
- NEVER DELETE demo media (Henry 2026-08-19b): keep every rendered artifact; move strays into `newt/demos/` instead of deleting. Kickoffs must not instruct workers to delete demo files.
- Demos are video-first per Henry's standing preference: mp4 via system ffmpeg or a live viewer, never image flipbooks.
- Orchestrator kickoffs for newt tickets must state the output path so workers write demos there directly.
- Applied 2026-08-19: moved `newt-nativeccd-hfield.mp4`, `stack.mp4`, `tendon.mp4`, `walk.mp4` from `newt/` into `newt/demos/`.

Related: [[orchestrator-worker-protocol]].
