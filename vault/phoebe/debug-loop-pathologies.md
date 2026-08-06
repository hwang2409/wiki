---
type: til
tags: [phoebe, process]
created: 2026-08-03
updated: 2026-08-03
---

# Debug-loop pathologies (PHO-15112 postmortem)

Context: the Modal containment lane took ~27 CI rounds (2026-08-03). Rounds 1-12 fixed seven real production bugs. Rounds 13-27 chased a failure CAUSED by round-13's own instrumentation: a result-marker env var tripped the runtime's `assert_environment_scrubbed()` (silent exit 2 before hello), and the crash-log probe wrote to /tmp inside bwrap's private tmpfs, so its evidence died with the namespace. Henry's zero-context second-opinion worker found it by reading `run_runtime` top-down.

Rules extracted (apply to any multi-round debug loop):

1. **When the failure signature changes right after adding instrumentation, the instrumentation is suspect #1.** Bisect by reverting to the last-known-good commit ENTIRELY — a single-commit revert that fails does not exonerate the other instrumentation commits.
2. **Enumerate, don't pattern-match.** Given a distinctive exit code / error string, grep ALL sites that can produce it before chasing the familiar one.
3. **A probe's silence is only evidence if the probe's channel is verified.** "Log absent" from a file inside a private tmpfs/namespace is a broken probe, not a datum. Verify the observation channel once before trusting absence.
4. **Sandboxed/namespaced targets: observe from INSIDE the namespace.** Outside probes (sandbox exec, tree listings) can all be green while the jailed process sees a different filesystem/env.
5. **Schedule a fresh-eyes pass every ~5 failed rounds.** Spawn a zero-context reader with only the current failure output and the entry-point code — no incident narrative. Narrative momentum fits new evidence into the standing story; fresh eyes read the code top-down.
6. **Env-scrub / security guards kill silently by design.** When a child process dies instantly with no output, check its environment against the parent's allowlist before anything else.

Related: [[env-vars-sync-for-credentials]] (false local-blocker classes), [[bazel-slot-shim-pathology]].

