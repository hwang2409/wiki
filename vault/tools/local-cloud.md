---
type: reference
tags: [tools, infra]
created: 2026-07-15
updated: 2026-07-15
---

# Local Cloud (self-hosted service stack)

Henry's end goal (stated 2026-07-15): run "local service" clones + off-the-shelf inference on the beefy GPU machine, delegating compute that paid services normally do; reachable while at work.

## Direction

- Build-vs-run split: CLONE where learning or control matters (search/storage — [[pufferclone]]-style); RUN off-the-shelf where the value is the model weights, not the plumbing (LLM inference, embeddings, whisper).
- Access from work: tailscale mesh, services bind tailnet address, no public exposure.
- Each service: daemonized (launchd/systemd), data under one root, uniform port map.

## Stack sketch (target)

| Layer | Choice | Status |
|---|---|---|
| Vector+FTS search | pufferclone (v1+v2 COMPLETE: HNSW/compaction/S3/non-blocking/budget/CLI, main@6bc5d64) | done |
| Metrics/observability | gauge — Prometheus-lite TSDB clone (v0 COMPLETE: Gorilla store, scraper, query+CLI, exporters; master@9d32f0b) | done |
| Ticket tracking | tix — Linear-clone CLI (TIX-1 merged) | done |
| Embeddings | local embedding server on GPU (e.g. TEI or ollama embed models) | not started |
| LLM inference | ollama or vllm on GPU box | not started |
| Blob storage | MinIO (also backs pufferclone S3Store from PUF-7) | not started |
| Wiki semantic search | vault notes → embeddings → pufferclone hybrid query | blocked on embeddings pick |
| Transcription | whisper.cpp server | maybe |

## Decisions

- **Hosting (2026-07-16): home box, not VPS.** GPU anchor makes VPS uneconomical (GPU rent >> owned hardware); tailscale already solves reachability; 24/7 via daemonization + auto-power-on + WoL; personal data stays on owned metal. VPS only if a genuinely public always-up endpoint appears (then: $5 ingress relay, no compute/data).
- **CLI-first (2026-07-16): every service gets a CLI before any web UI.** Terminal is the primary client 99.9% of the time; web UIs deferred until a real need. First implementations: `puf` CLI (PUF-12), `tix` ticket tracker (standalone Linear clone, ~/me/fun/misc/tix, TIX-1).

## Clone pipeline (Henry 2026-07-16: keep this section updated with ideas continuously)

- **Next up after gauge — evaluate**: (1) durable workflow engine (Temporal-lite): deterministic replay, retries, timers — deepest learning candidate; would power vault-ingest pipelines + agent jobs; (2) LSM KV store (Redis/RocksDB-lite): foundational storage-engine learning, overlaps pufferclone WAL/segment lessons — risk: redundant with what pufferclone already taught.
- Other candidates surveyed 2026-07-16: event log/queue (Kafka-lite — no producers yet, revisit when pipelines exist), CI/job runner (pairs with remote-Bazel backlog), secrets manager (Vault-lite — relevant at tailnet exposure), container registry.
- Clone-pick heuristics that emerged: daily-utility-first beats learning-density when stack has a gap; don't build infrastructure without a customer (event log lesson); prefer targets whose value is engine design, not weights/network moat.

## Backlog (uncommitted ideas)

- **Remote Bazel offload to GPU box** (2026-07-16, Henry unsure — parked): 8 local Bazel-test workers slow the Mac. Tiered options if revived: (1) shared remote cache — `bazel-remote` container on GPU box, workers add `--remote_cache` to ~/.bazelrc, zero workflow change; (2) `rbazel` runner CLI — rsync worktree to box, ssh `bazel test`, stream results back, per-branch dirs keep analysis cache warm; (3) full RBE (`--remote_executor` + buildbarn/NativeLink), most setup. Constraint all tiers: box is Linux, workers macOS — phoebe tests must pass on Linux (10-min probe first); cache keys are platform-scoped. Gate reruns keep `--cache_test_results=no` regardless.

## Notes

- Pufferclone S3Store (PUF-7) makes MinIO the natural first infra piece — one blob backend for everything.
- Embeddings service unblocks the deferred wiki-semantic-search thread (see [[turbopuffer]] verdict: local sqlite-vec OR pufferclone now that it exists).
- GPU box duties: inference + embeddings; search/storage services are CPU-cheap and can live anywhere on the tailnet.
