---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-22
---

Todo:

- [P2] wiki: compact mermaid artifact preview illegible for large diagrams — styles.css:6684 caps svg at 400px, scale-to-fit squeezes text to ~3px; fix = crop + click-to-inspect or min-scale floor (pan/zoom detail exists). File as WIKI ticket when Linear reauthed
- [P2] PHO-13800 customer_churned_date timing unreliable (19/29 in one entry window; zero churns Jan-Apr) — backfill true dates or document entry-date semantic; related PHO-13735
- [P2] PHO-13763 PARKED by Henry 2026-07-15: PR 11383 drafted at 909ef288ce, gate-passed (CI green, 0 threads, sol REVIEW4 merge-ready), worker archived — resume = un-draft, re-verify freshness vs main, merge
- [P3] WIKI-126: surface rebrand — rename app-facing identity only (app display name, window title, README header); NAME NOT YET CHOSEN by Henry, blocked until he picks. Internal identifiers stay (repo, wiki CLI, WIKI-* tickets, MCP names, vault paths — codename doctrine, Henry 2026-07-15)
- [P2] amd-shadow invalid index: KORGAN owns fix (PR 11416 closed w/ evidence); prod index still INVALID 0-byte — watch deploys
- [P2] PHO-13826/27/28 exe.dev sandbox arc: enable (SSH key secret + smoke) -> ownership fix -> TTL sweeper; PHO-13829 egress design backlog. Surface merged since PHO-13073/#10608, dormant on missing key
- [P3] wiki backend /metrics endpoint (gauge follow-up)
- [P3] pufferclone /metrics endpoint (gauge follow-up)
- MITMWEB-F4: full restyle of mitm-inspector web UI for clarity — adopt theme/layout language of ~/me/fun/wiki frontend; spawn fable worker after I1 integration merges — owner (misc)
- Slack admin agent access for Corey Grissom — confirm with Corey his @phoebe.work email + provisioning plan, then either (a) have him sign into app.phoebe.work with @phoebe.work Google Workspace so Kratos provisions the user row + flip admin=true, or (b) one-time hotfix INSERT app.users row + backfill org_slack_user_mappings.user_id for slack_user_id U0AGES89BLZ (2 org rows: Phoebe Home Care + Orchard St. Homecare, both currently user_id=null). Root cause: no app.users row exists at all — his Slack identity is known but unmapped. Full diagnostic + proposed SQL in phoebe session 2026-07-17.
- [P1] PHO-14029 prod dedupe backfill (bun run people:dedupe-backfill --write) + PHO-14033 unique index migration on people(lower(email)) — EOD 2026-07-22
- [P2] WIKI-144 badge sweep: audit + unify status badges across dashboard, session-header, artifact-list — reuse WIKI-143 design tokens; remove redundant variants, consistent color/weight/shape
- [P2] WIKI-147 session-list unread dot: left-edge dot on sessions with events since last-viewed_at; per-session last_viewed_at persisted (backend + frontend); clears on session open
- [P2] WIKI-148 composer slash menu: inline /steer /spawn /gate /archive chips with arg hints + tab-complete; parses to structured MCP call; fuzzy filter as user types
- [P2] WIKI-149 code-block copy button + diff artifact kind: hover copy on ``` blocks; new artifact renderer syntax-colors +/- with hunk headers; register kind:diff in artifact router
- [P1] WIKI-150 polish census: audit every visible Wiki.app surface (panel/header/modal/empty/loading/error state), rate polish 1-5, catalog copy leakage + micro-inconsistency + dev cruft; census doc in vault/wiki/polish-census.md fans out into 8-12 small polish tickets
- [P1] WIKI-151 nav + sidebar IA: ribbon zones, sidebar shell, file/workspace states, tab header, session list — three grouped ribbon zones, one sidebar frame across modes, Active/History run grouping, no silent workspace fallback (depends WIKI-147)
- [P1] WIKI-152 agent-session chrome: header, provider inspector, action-required card, composer help, footer/status rail — kill 'Provider stream', raw/normalized counts, request IDs, Unknown 0, format/token telemetry, tmux punctuation from default chrome; diagnostics in Run details (depends WIKI-148)
- [P1] WIKI-154 runs management productization: Agents page/cards/banners/actions/session preview/spawn+replace dialogs — Active/History hierarchy, decision-relevant fields only, IDs/tmux/log-paths in Technical details, provider/auth notices state user impact + next action
- [P1] WIKI-155 session + dashboard state completeness: explicit zero-event/working/error variants with recovery, dashboard table skeleton, both empty variants contextual, last-good content survives refresh failure
- [P2] WIKI-156 artifact shell + renderer-state polish — quiet inline header, no coming-soon controls or ID-prefix titles, shared loading/error/fallback component (depends WIKI-144, WIKI-149)
- [P2] WIKI-157 utility-page refinement: activity/graph/health/token usage — title+loading+empty+error+retry everywhere, graph keyboard/noncanvas access, git/CLI terminology secondary, no false-zero token data
- [P1] WIKI-158 global resilience + lifecycle states: first run, backend down, provider auth, update available, notices — coherent first-run path, backend outage != empty vault, persistent sign-in state, all states announce success recovery
- [P2] WIKI-159 keyboard + dialog accessibility: kanban/dashboard filters/destructive dialog/context menu — keyboard equivalents for drag/double-click, listbox+menu+dialog semantics complete, focus trap+restore, destructive copy describes outcome+recovery
- [P2] WIKI-160 design-token convergence + shared controls: spacing/radii/motion/icons/shadows, settings+status components — one spacing/radius/motion vocabulary, no dup radius aliases, theme-token shadows, weights cap 600, primitives everywhere (rebase after WIKI-144)

In Progress:


- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P2] [PHO-13669](https://linear.app/phoebework/issue/PHO-13669): Core email/account-book gaps — cdx:PHO-13669 (gpt-5.6-terra)
- PHO-13763 per-org scratchpad (worker cdx:PHO-13763, run bd90b506)
- [P2] PHO-13830 live EHR record fetch tool (admin agent) — worker live
- [P2] PHO-13832 shift classification + calendar artifact (admin agent) — worker live
- [P2] PHO-13826 ModalSandboxBackend + PHO-13827 sandbox ownership — workers live (Modal replaces exe.dev for v0; 13828 lifecycle + 13829 egress design queued)
- mitmweb rebuild: scope and build a clearer live proxy-traffic inspector — owner (misc)
- WIKI-135 dashboard: implementation workers only (drop reviewers/one-shots) — owner cdx:WIKI-135 (luna)

Backlog:

- [PHO-11469](https://linear.app/phoebework/issue/PHO-11469): re-triage Slack-thread alert investigation
- [PHO-12425](https://linear.app/phoebework/issue/PHO-12425), 12426: sandboxed outreach start + e2e dogfood
- [PHO-11256](https://linear.app/phoebework/issue/PHO-11256)–11260: texQL series
- [PHO-11269](https://linear.app/phoebework/issue/PHO-11269)–11272: canonical data products series
- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274), 11275: trace comparison + prompt/context diffing
- [PHO-11251](https://linear.app/phoebework/issue/PHO-11251), 11264, 11267: artifact actions, playbook authoring, known-issue memory
- [PHO-12211](https://linear.app/phoebework/issue/PHO-12211): vendor record-and-inject primitive for sandbox probes
- [PHO-11598](https://linear.app/phoebework/issue/PHO-11598): Snowflake QA/feedback + trends tool
- [PHO-12134](https://linear.app/phoebework/issue/PHO-12134): Voice QA inspection support
- [PHO-11727](https://linear.app/phoebework/issue/PHO-11727): subagents as nested tasks in /admin/agent
- [PHO-11539](https://linear.app/phoebework/issue/PHO-11539): admin agent as read-only MCP server
- [PHO-11540](https://linear.app/phoebework/issue/PHO-11540): daily run review summary for tool quality
- [PHO-11651](https://linear.app/phoebework/issue/PHO-11651), 11629: playbook input validation + progress events
- [PHO-11535](https://linear.app/phoebework/issue/PHO-11535): fact/inference evidence tiers for RCA answers
- [PHO-11231](https://linear.app/phoebework/issue/PHO-11231): Phoebe Home Care seed data for visual testing
- [PHO-12982](https://linear.app/phoebework/issue/PHO-12982), 12757: reference tickets (harness doctrine, tool brainstorm)
- [P1] [PHO-13138](https://linear.app/phoebework/issue/PHO-13138): Phase 2 admin-owned tables — plan approved (ticket comment = contract, 8-PR series); cdx:PHO-13138 worker on PR1 (schema/roles/deny-tests)
