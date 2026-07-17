---
type: decision
tags: [mitmproxy, architecture, security]
created: 2026-07-16
updated: 2026-07-16
---

# Public-Hook Adapter Architecture

**Decided:** Keep stock `mitmdump` as the proxy engine; a small addon using documented hooks emits a project-owned, versioned, bounded/redacted stream to an independent backend and UI.

**Why:** Anthropic SSE must remain incremental and byte-identical; secrets must be removed before browser transport; proxy forwarding cannot depend on UI speed; upstream private schemas must not become our contract.

**Rejected:** Fork mitmweb — permanent upstream merge burden and inherited UI/backend coupling.

**Rejected:** Wrap mitmweb REST/WebSocket — private API, unbounded flow retention, secret-bearing summaries, no durable cursor/ack, and no chunk-level SSE feed.

**Rejected:** Tail `.mitm` files — completed-flow persistence is not a live lifecycle/control transport.

**Revisit if:** Documented hooks cannot provide a byte-preserving bounded stream on the pinned mitmproxy release after the S0/B2 spike.
