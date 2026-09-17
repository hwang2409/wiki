---
type: reference
tags: [phoebe, v3-agent, sandbox, modal]
created: 2026-08-26
updated: 2026-08-26
---

# v3 sandbox workspace: volume-backed artifacts proposal

Direction agreed with Henry 2026-08-26 (pending spike validation). Replaces
the copy-based durability layer described in
[[v3-agent-modal-sandbox-design]] if the spike passes.

## Problem

Current design copies workspace files between the Modal sandbox and
Postgres (persist + rehydrate). Two objections (Henry):

1. Syncing is a failure mode. The ideal design has no sync step at all.
2. Large artifacts in the production Postgres database are bad (blob
   bloat in the OLTP store).

## Proposed architecture

Two mounts per run inside the org sandbox:

- `/workspace` -> container-local disk. Ephemeral scratch. Dies with the
  sandbox; recycling stays the garbage collector. No storage policy.
- `/workspace/artifacts` -> the run's directory on a per-organization
  Modal Volume. Durable BY LOCATION: writing there is persistence,
  remounting after a recycle is recovery. Holds spill files, receipts,
  and anything the model deliberately keeps (any file type).

Deleted if adopted: `durable.py`, the `agent_run_workspace_files` table,
hydration + generation counter + workspace fences, the `.jsonl` register
sweep (location convention replaces suffix convention).

Storage governance: per-run quota on the artifacts dir (write-time check
for tool code, periodic du for bash writes), per-org volume quota as
backstop.

## Retention (revised 2026-08-26, Henry)

NO wall-clock TTL. Artifacts live exactly as long as the conversation
that references them — the same rule chat attachments follow. The
current 7-day TTL is an incoherent cliff: inline results persist forever
with the conversation while spilled results (same data, one byte over
the threshold) die in 7 days, and reopened chats hold dead ws://
pointers. Sweep triggers on conversation deletion/archival; a wall-clock
sweep remains only for orphaned run dirs whose conversation is gone.
Affordable because the durable set is quota-capped per run (bulk scratch
never hits the volume). Optional future cold tier (S3 after N idle days)
deliberately deferred — it reintroduces a background move step.

Unchanged: bwrap per-run isolation, block_network, concurrency budget,
inline results, `ws://` pointer semantics, uploads projected from the
attachment store.

## Why not plain "persist everything on a volume"

Recycling currently garbage-collects bulk scratch for free (e.g. 100
users x 10 PDFs/day). A fully volume-backed workspace makes junk durable
and needs policy for everything. The two-mount split keeps ephemerality
as the default and durability opt-in by location.

## Storage backend research (2026-08-26, verified against docs + modal client source)

- `NetworkFileSystem` is DEPRECATED — not an option.
- Modal Volumes (v1 and v2) are commit-based (background commits every few
  seconds, final commit on termination, pull-model reload, last-write-wins
  per file). Commit = persist and reload = hydrate: building on them
  recreates sync. DISQUALIFIED as primary.
- WINNER: `CloudBucketMount` — mount a phoebe-owned S3 bucket into the
  sandbox (AWS Mountpoint under the hood):
  - Durable on file close; S3 strong read-after-write consistency; a
    replacement sandbox sees completed files immediately. No commit step.
  - Crash-atomic: an interrupted write is an incomplete multipart upload —
    the object never appears (add an S3 lifecycle rule to reap incomplete
    multiparts).
  - Client-source evidence (modal/cloud_bucket_mount.py,
    cloud_bucket_mounts_to_proto): the mount is config in the
    sandbox-create request, realized by Modal's runner OUTSIDE the guest —
    coexists with block_network=True; credentials travel as
    credentials_secret_id on the mount proto and never enter the guest
    env; `oidc_auth_role_arn` allows scoped IAM role auth with no
    long-lived keys; `key_prefix` gives native per-org prefix mounts.
  - Constraints (Mountpoint): no append, truncate-only writes, no rename,
    sequential-read optimized. Artifacts are write-once; index.jsonl is
    already rewritten whole under the writer lease; bash keeps must use
    `>` or `cp`, not `>>`/`mv`.
  - Storage lives in phoebe's AWS account: our KMS, our lifecycle rules
    (conversation-lifetime retention enforceable natively), no
    Modal-storage BAA question.

## Spike questions (must pass before adoption)

1. Confirmation run: DONE 2026-08-26, all pass (live run against Modal
   dev + a scratch S3 bucket, script preserved at
   /tmp/modal_mount_spike.py): write+read through the mount from a
   block_network=True sandbox works; guest env contains zero AWS
   variables; egress stays blocked; append correctly rejected
   (truncate-only); a FRESH sandbox sees the file immediately after the
   writer's death; object confirmed in S3. Scratch bucket deleted after.
2. Kill-mid-write: DONE 2026-08-26 — SURPRISE: graceful terminate at
   ~40% of a 100MB write COMPLETED a 46MB torn object to S3 (no stray
   multiparts). NOT crash-atomic. Hard design rule adopted: a ws:// path
   is only real once its index entry exists (index appended after the
   artifact write completes; readers resolve via index and validate
   recorded size). Torn objects = unreferenced orphans, swept by
   lifecycle rules.
3. Latency: DONE 2026-08-26 (measured, one region pair) — mount vs
   local: 1MB write 239ms/3ms, 10MB 428/6, 50MB 777/12; reads 1MB
   33ms, 10MB 162, 50MB 656; first-100-lines paging of 50MB JSONL 51ms;
   50 small files 9.8s (~195ms each — batch, never scatter); index
   read-modify-rewrite ~225ms/cycle; listing 26ms; mount adds ~93ms to
   sandbox cold start; cross-sandbox freshness 221ms.
4. key_prefix per-org mounts: verified in client proto + used live.
   mv: "Function not implemented" (confirmed); cp works; append
   rejected. Bash rule: `>` and `cp` only.
5. Remaining for the implementation ticket (operational, not
   architectural): OIDC role auth (oidc_auth_role_arn) instead of keys,
   bucket region pinned to Modal's primary region, lifecycle rules
   (abort incomplete multiparts, orphan sweep, conversation-lifetime
   deletion), S3 request-cost note (PUT ~$5/M; per-run quota bounds it).
   Spike scripts: /tmp/modal_mount_spike.py, /tmp/modal_mount_spike2.py.

Fallback if the spike fails: S3 durable-store adapter (the store is
already pluggable) + shorter TTL. Fixes the Postgres objection, shrinks
but does not remove the sync objection.
