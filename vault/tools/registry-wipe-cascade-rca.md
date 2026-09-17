---
type: reference
tags: [wiki-app, rca, agent-runtime]
created: 2026-08-19
updated: 2026-08-20
---

# Registry wipe cascade RCA (WIKI-353 extension)

**Symptom (Henry, 2026-08-20 ~01:20Z):** workers show at sidebar top level ("ready", ungrouped) instead of nested under their orchestrator. Recurs without app restarts.

## Root cause

`/tmp/agent-registry.json` is a whole-file read-modify-write projection with no rebuild-on-miss. Once the file is missing or `{}`, the supervisor's own spawn path perpetuates the wipe:

1. **`RunStore.create()` (store.py ~2058-2172)** reads the registry at entry; `_read_registry()` returns `{}` on FileNotFoundError. create() then writes the whole file back containing ONLY the new agent — every other live agent's entry is silently discarded. It never consults the durable `runs/*/run.json` it owns (reconcile runs only in `RunStore.__init__`).
2. **`abort_start()` → `_restore_start_snapshot()` (store.py ~1132)** after a failed provider start pops the new agent again and writes `{}` (3 bytes) — or, when the durable snapshot recorded `registry_file_present: False`, **unlinks the file**, re-arming step 1 for the next spawn.
3. Transition/steer writers are guarded (`current.run_id == record.run_id` on an existing entry) — they never re-create wiped entries. So idle workers stay unassigned forever; only respawned/replaced agents reappear.

Sidebar effect: entries missing from the registry render via the status-file drift path (main.py ~2444) with hardcoded `orch: None, registered: False` → top-level "ready" rows. Frontend groups by `worker.orch === orch.id` (agents.tsx ~1450).

## Evidence (2026-08-20)

- Three wipes with no backend restart (backend up since 22:26Z): ~00:05:20Z, 00:22-00:59Z, 01:02-01:19Z. Windows match failed provider starts (ZETA-3 x3, ZETA-3-REVIEW1, PHO-16397-REVIEW2 — bare `provider start failed:`, the fd-exhaustion era).
- phoebe orch saw the file at 3 bytes `{}\n` mode 0600 at 00:05Z — byte-exact fingerprint of the store's `_atomic_write_json({})`; CLI/orch scripts write 2-byte indent-1 JSON, so the store wrote it.
- wiki orch reseed at 01:19:57.200Z printed `pre: ['ZETA-3-REVIEW1']` — only the agent spawned 82ms earlier (01:19:54 retry after the 01:19:05 abort). Exact cascade signature.
- `command_projection` rows for wiped agents stayed intact while registry entries vanished — every legit store removal also clears the projection, proving no legit path removed them.
- Isolated repro (create A → delete file → create B → abort B → create C) reproduces: `[A]` → `[B]` → `{}\n` (3 bytes, 0600) → `[C]`.

## Ruled out

- Backend test-suite gates: full backend suite on main run against a canary registry (env-redirected) — canary untouched. PR-branch test additions (357/373/hardening) clean on static scan.
- uuid5 run-id archive collisions: live run ids do not appear in committed archives.
- wiki CLI `read_registry()` swallow-to-`{}` (wiki:380): real footgun (any OSError → full wipe on next mutating command) but format fingerprint clears it for these wipes.
- Orchestrator reseed scripts: they preserve existing keys (`if ticket in reg: continue`); they only under-repair (each orch reseeds its own lanes), which is why victims were "everyone else's idle workers".

## Fix direction (for the ticket)

- `_read_registry()` returning `{}` on a missing file is the landmine: create()/restore should treat a missing/empty registry as "rebuild from `runs/*/run.json`" (call `_reconcile_registry_from_runs()` on read-miss), not "there are no agents".
- Guarded transition writers could self-heal: re-project the entry from the RunRecord when it is missing instead of skipping.
- Initial trigger (restart deleting the file) is the original WIKI-353 scope; move the registry out of `/tmp` or make it a pure projection.

## Repair playbook delta

Reseed as in [[orchestrator-worker-protocol]] 2026-08-19b, plus: skip run.json whose `state` is terminal, and NEVER seed orchestrator runs (agent_id = wiki/phoebe/tooling/zeta) as top-level worker keys — they live in `_orchestrators` only (two-live-identities hazard). Applied 2026-08-20 ~02:3xZ: reseeded PHO-16380, ZETA-3 (NEWT-34 already reseeded by tooling orch; PHO-16397, ZETA-2-FIX1 legitimately archived meanwhile).

**Recurrence #4 (2026-08-20 ~04:5xZ, post-rebuild boot):** wipe hit the NEW app build's first fleet session — every orchestrator (all four -dev incarnations) and all 5-6 live workers deregistered; file reduced to `{"_orchestrators": {}}`-equivalent. Window again matches multi-orch concurrent spawn load ("7 active workers; soft cap 5" warnings). tooling-dev full-fleet reseed from non-terminal `runs/*/run.json` with mapped keys restored all lanes in one atomic write; spawn retried clean with a fresh request_id. Confirms the rebuild did not carry a fix — WIKI-353 fix direction above still open. run.json key mapping for reseed scripts: `orchestrator_id`→`orch`, `provider` codex/claude→`kind` cdx/cc, `created_at`→`spawned_at`, `transcript_path`→`transcript`, `worktree`→`cwd`, log = `<run-dir>/raw.jsonl`.
