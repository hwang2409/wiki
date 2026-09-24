---
type: reference
tags: [wiki-app, performance, archive, rca]
created: 2026-09-24
updated: 2026-09-24
---

# Palette archive CPU path (2026-09-24)

**Cause:** Wiki warms palette artifacts at backend startup. Each palette search then calls `read_artifact_events()`, which parses every archived `events.jsonl` file. The endpoint also opens every archived session body through `list_archived(limit=None)` on each query. This path is separate from the knowledge index and `/api/agents` fixes in [[knowledge-index-cpu-rca-2026-09-24]] and [[agents-list-cpu-rca-2026-09-24]].

## Evidence

- The local archive held 3,681 `events.jsonl` files and used 60 GB on 2026-09-24. The recent 40 committed sessions held 128 MiB of event JSONL and no artifact events. A source inspection shows the startup thread and palette endpoint both invoke the full archive reader.
- The palette sends a request 80 ms after each query change. Reading all archived session bodies with the PR #300 branch took 0.96 seconds on a cold call and 0.56 seconds in an earlier call. The full artifact scan would add far more work. These are local path measurements, not a measured live request latency or CPU share.
- The active backend still runs the old app build. Its current high CPU cannot be assigned precisely to palette without a stack sample during a palette search; the startup thread is a plausible concurrent contributor.

## Decision and fix

- Henry chose recent archived artifacts over a complete palette artifact history. Agent run history remains available in the agent archive and session UI.
- Remove the startup palette scan. Search live SQLite artifacts and at most the 40 newest committed archived sessions, with a 128 MiB archive-read cap. Cache each unchanged archived file's artifact events. Reuse the full archived session list across a short typing burst.
- Local direct measurements from the branch: first bounded artifact read 0.23 seconds; second read 0.13 seconds. The full archive session list took 0.96 seconds first, then 0 seconds from the cache. The installed app has not been updated.
- PR [#301](https://github.com/hwang2409/wiki/pull/301) merged as `63bdff19`. The installed app still runs the old build.
