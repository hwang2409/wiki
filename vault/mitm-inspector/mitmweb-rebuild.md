---
type: campaign
tags: [mitmproxy, proxy, frontend]
created: 2026-07-16
updated: 2026-07-17
---

# Mitmweb Rebuild

Build a clearer, high-density local inspector for live mitmproxy HTTP(S) flows, starting with Anthropic reverse-proxy traffic.

## Locked MVP

| In | Out |
|---|---|
| Loopback-only reverse proxy; live virtualized flow grid; paired request/response inspector; JSON/text/SSE/hex views; timing; native-filter-compatible search | Replay/edit/intercept controls; remote access; raw persistence/export; TCP/UDP/DNS; cloud/team features |
| Public-hook capture addon; versioned local IPC/API; redaction before transport; bounded memory retention | Forking mitmweb; consuming its private REST/WebSocket API; storing raw `Flow` objects |

- Working path/name: `~/me/fun/misc/mitm-inspector` / `mitm-inspector` until release naming.
- Default retention: memory only, newest 2,000 completed flows or 30 minutes, 128 MiB global body budget, 1 MiB captured prefix per side.
- Sensitive headers/query values are irreversibly redacted before leaving the proxy process; bodies remain sensitive and appear only on selection.
- UI: virtualized 28px flow grid over paired inspector; follow-live pauses when navigating history; request/response/error lifecycle ordering is not assumed.
- Delivery graph: `S0 → {B1 runtime, B2 capture, F1 shell, F3 inspector} → {B3 API, F2 flow grid} → I1 integration/security/perf → P1 packaging`.

## State

Planning complete from supervisor runs `MITMWEB-ARCH`, `MITMWEB-INGEST`, `MITMWEB-UX`, and `MITMWEB-SCOPE`. Foundation S0 merged locally at `420f2e2`. B1 runtime merged at `6214643` after five independent review rounds. F3 standalone paired inspector merged via `7e78199` after six rounds. F1 connection client, source/cursor state machine, follow-live shell, reconnect/resync UX, dense responsive styling, and mounted DOM coverage then merged via `fd9e251` after six rounds. The combined tree passes 242 Python and 82 frontend tests plus strict types, lint, production build, and desktop/mobile browser gates. B2 capture/store remains in its final transaction/loadability repair loop. Architecture follows [[public-hook-adapter]]. Verified against [mitmproxy 12.2.3](https://github.com/mitmproxy/mitmproxy/releases/tag/v12.2.3), [event hooks](https://docs.mitmproxy.org/stable/api/events.html), and [Anthropic streaming semantics](https://platform.claude.com/docs/en/build-with-claude/streaming).
