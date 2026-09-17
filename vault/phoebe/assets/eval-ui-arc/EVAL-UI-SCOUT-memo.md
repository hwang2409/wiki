---
type: reference
tags: [phoebe, evals, memo]
created: 2026-09-17
updated: 2026-09-17
---

# generated ui eval scout memo

## executive summary

`render_dynamic_ui` is a real end-to-end surface. The agent chooses a component and data source. The backend validates the data, writes an immutable S3 snapshot, and returns a small receipt. The web app parses that receipt, requests paged rows, and renders a table or domain card.

The current tests cover most transport and schema failures. They do not run the agent and then ask whether the selected component, columns, rows, ordering, title, or actions answer the user’s question. They also do not load a real S3-backed receipt in a browser.

The best first eval is a small golden-case family. Each case should seed a known organization, run the real V3 agent, inspect the persisted receipt and bytes, and use deterministic checks for structure and expected facts. A small browser pass can then check the final rendered result for a few high-value cases.

## 1. end-to-end pipeline

The current runtime flow is:

```python
user_question -> query_or_run_bash -> render_dynamic_ui
render_dynamic_ui -> validate rows -> upload immutable S3 bytes
upload receipt -> tool output event -> database artifact metadata
receipt -> frontend parser -> workspace-file API -> paged rows -> table/card
```

### agent contract and runtime

`libraries/python/phoebe_v3_agent/tools/interaction/render.py:325-404` defines the model-facing contract. It says that `render_dynamic_ui` is the only tool that turns data into result cards or tables. `query` and `run_bash` produce data. Action tools produce their own cards.

The data components are `table`, `shifts`, `caregivers`, `client_detail`, and `agency_rules`. The same module also names `outreach_detail` and `recommendations` as reference components at `:97-127` and `:555-578`. Those components use an `outreach_id` and do not publish row data.

The contract requires exactly one data source at `render.py:205-255`:

- `source_path` plus `format` for a workspace file.
- `rows` for inline JSON objects.

The runtime normalizes inline and query-backed domain rows at `libraries/python/phoebe_v3_agent/tools/interaction/query_rendering.py:32-72`. It maps common query aliases for shifts, caregivers, clients, and agency rules. It rejects conflicting nested and sibling entity values at `:140-170`.

`execute_render` validates the component and source, reads a workspace file when needed, normalizes inline rows, and validates the result at `render.py:205-280`. It creates a runtime-named path and uploads the exact bytes to private object storage at `:281-297`. It returns only a compact receipt at `:314-322`.

### validation and component contracts

`libraries/python/phoebe_v3_agent/middleware/dynamic_ui.py:31-205` defines the component registry and strict domain row models. Domain models forbid extra fields and use strict types. Supported contracts include:

- shifts: `shift_id`, `client`, `start_time`, `end_time`, `status`, `assigned_caregiver`.
- caregivers: `caregiver_id`, `display_name`, `active`, `roles`, `office`, `city`, `state`, `qualification_tags`.
- client detail: one row with client identity and profile fields.
- agency rules: one concise `rule` string per row, capped at 240 characters.
- table: arbitrary consistent CSV or JSONL rows.

`validate_render_file` at `dynamic_ui.py:242-339` parses the file, checks its format, collects columns, validates context IDs, validates domain rows, enforces exact row counts, rejects empty output by default, and computes the content hash and page offsets.

The main limits are 10 MiB per render file at `:255-260`, 100,000 rows in the row iterator, and 128 distinct columns at `:280-297`. Detail components require one row at `:311-316`. Invalid data becomes a model-visible tool error at `render.py:298-313`.

### sandbox workspace

`libraries/python/agent_sandbox/workspace.py:14-17` sets the `/workspace` mount and the 10 MiB spill limit. `RunWorkspace` derives a stable run-owned directory from organization, run, mode, and an HMAC token at `:27-130`. Path validation rejects absolute paths, traversal, unsafe segments, and similar inputs at `:74-94` and `:133-135`.

`libraries/python/agent_sandbox/sandbox.py:168-278` runs commands in a hydrated, pooled sandbox. `write_file` and `read_bounded_file` use the same run workspace and retry on recyclable sandbox failures. Bounded reads are symlink-safe and capped at `sandbox.py:464-526`. The confinement layer binds only the run directory as writable storage.

### snapshot and event persistence

`libraries/python/phoebe_agent_render_artifacts/byte_store.py:60-122` stores bytes under a key containing organization, mode, run, content hash, and relative artifact path. The object store is private, bounded, and read through the shared S3 transport.

`libraries/python/phoebe_agent_render_artifacts/persistence.py:18-95` reads the receipt from a successful `render_dynamic_ui` tool output and inserts `AgentRenderArtifact` metadata. The database row contains the run, turn, path, S3 key, hash, byte size, format, and row count. The migration is `database/migrations/20260916153857_add_agent_render_artifact_snapshots.sql`.

`libraries/python/agent_event_framework/tool_execution.py:572-614` persists the tool output and then binds the artifact’s event ID. The upload happens inside the tool before the event transaction completes. Therefore, a later transaction failure can leave an unindexed S3 object. The database row and event binding remain the authoritative read path.

### API read path

`services/api/routes/organizations/phoebe_agent_runs/workspace_files.py:62-143` serves `/workspace-files/{file_path}`. With an event ID for a render event, it reads the immutable snapshot. Without that binding, it can fall back to the mutable workspace file. The route paginates rows, enforces a 200-row request cap, and enforces a 256 KiB response cap at `:40-60` and `:145-234`.

`services/api/routes/organizations/phoebe_agent_runs/render_artifact_reading.py:46-114` authorizes the run, checks the event receipt, finds matching database metadata, reads S3, and verifies byte size and SHA-256. Missing snapshots return 404 or 409. Storage and integrity failures return 503.

`services/api/routes/organizations/phoebe_agent_runs/ui_context_validation.py:172-232` binds a UI context to the run, event, path, and snapshot. `:235-288` then parses rows, checks row count, enforces one-row detail results, and confirms visible entity IDs belong to the saved result.

### frontend rendering

`apps/web/lib/agent_render_artifact.ts:3-135` parses the receipt. It accepts strict path, format, size, count, column, hash, and page-offset fields. It has separate parsers for generic rendered rows, agency rules, outreach detail, and recommendations.

`apps/web/features/phoebe_chat_v3/registry/definitions/tool_output.tsx:330-397` converts a committed tool output into a typed presentation. Domain data becomes a titled table with a source event and optional UI context. Agency rules become a compact preview. Untrusted raw rows are not projected into the presentation.

`apps/web/features/phoebe_chat_v3/registry/definitions/tool_call.tsx:680-717` dispatches table and agency-rule presentations to their UI resources.

`apps/web/features/phoebe_chat_v3/ui/generated_table.tsx:82-165` loads rows with `useAgentRunWorkspaceFileRows`, passes the source event ID, and batches shift outreach state. The hook is `apps/web/hooks/use_agent_run_workspace_file_rows.ts:63-210`. It calls the workspace-file API, keeps page state, and does not retry failed requests.

`apps/web/features/phoebe_chat_v3/ui/primitives/generated_table.tsx:34-115` hides ID columns, labels snake-case keys, renders client objects as pills, and formats dates, lists, nulls, and long values. Its load failure text is exactly `This table could not be loaded.` at `:466-470`. A later page failure shows `Could not load the next page.` at `:693-713`.

Agency rules use `apps/web/features/phoebe_chat_v3/ui/agency_rules_preview.tsx:25-69`. The component loads rows, rejects malformed or empty content, shows only the first two rules, and links to playbooks.

### charts and other artifacts

`render_dynamic_ui` has no chart component. The supported render components are tables, domain tables, agency-rule rows, and live outreach or recommendation references. Visual charts use the separate data-analysis workflow. `libraries/python/phoebe_v3_agent/skills/data_analysis.md:55-99` describes matplotlib or seaborn exports, and `:160-180` describes summary files, images, and reports delivered through chat attachments. A chart eval should be a separate attachment-eval family unless that scope changes.

## 2. failure modes

### pipeline breakage

- The model can send unknown fields, an unsupported component, both sources, no source, an invalid title, or an invalid selection action. The runtime catches these as tool errors. `render.py:216-232` covers the main checks.
- A source path can be missing, absolute, traversing, a symlink, non-regular, or larger than the cap. Workspace and render tests cover many cases, but a live eval is needed to prove the model recovers and still produces a result.
- A query or Bash writer can create malformed JSONL, malformed CSV, inconsistent columns, non-standard JSON constants, or the wrong format. Validation catches these before upload.
- A domain result can miss required fields, use wrong strict types, contain extra fields, contain invalid UUIDs, or violate exact row count. The domain validator catches these.
- A valid generic table can still exceed frontend limits. Backend validation allows up to 128 columns, while the frontend receipt schema caps columns at 64. This is a contract seam.
- An empty result is rejected by the render validator unless a caller explicitly enables empty output. This makes a valid “no matching records” answer a possible failure path.
- Sandbox hydration or recycling can cause a source read to fail. The sandbox retries some failures. The model sees a not-allowed tool error if the service remains unavailable.
- S3 upload can fail. The tool reports that it could not save the snapshot. A database transaction can fail after upload, leaving an unindexed object.
- Event binding, run mode, organization access, path, or receipt metadata can disagree. The API returns authorization, 404, 409, or integrity errors.
- The frontend can reject a valid-looking receipt because its Zod schema has drifted from the Python contract. The agency-rule path is already separate from the generic table path.
- The frontend can receive a missing snapshot, an API error, or a page error. It shows a generic table failure or next-page failure. The hook does not retry.
- A malformed row can reach generic frontend formatting because the generic table parser does not validate every row value. The cell formatter has JSON fallbacks, but unexpected object shapes can still produce poor display or runtime errors in client-pill code.
- Selection context can fail if visible IDs do not belong to the saved snapshot. The API protects the action path, but the user sees a failed interaction rather than a rendered result.

### wrong content

- The agent can answer in prose or show a count when the coordinator needs a result table. Current general-chat evals catch some text omissions, but they do not require a rendered artifact.
- The agent can choose a generic table when a domain component fits. The runtime only suggests a domain component. It does not enforce the better choice.
- The agent can choose the right component but wrong columns, title, date window, timezone, status filter, ordering, grouping, aggregation, or top-N cutoff. Shape validation cannot detect question mismatch.
- A query can return valid rows with a wrong join, filter, or aggregation. The renderer accepts them because they satisfy the generic or domain schema.
- Rows can be duplicated, omitted, stale, or sorted in a way that hides the answer. The receipt records count and hash, not semantic completeness.
- The result can expose technical columns, hide useful context, or use an unclear title. The browser renders it, but no current eval grades coordinator usefulness.
- A client object can have a valid display name but no ID. The table remains readable, but it cannot open the client profile.
- Selection actions can be structurally valid but semantically wrong, unsupported, or distracting. The runtime checks shape and selectable rows, not whether the action fits the question.
- Agency-rule rows can be concise and schema-valid while changing the meaning of the source guidance. No current eval compares rendered rules with source playbook meaning.
- A live outreach or recommendation card can be the wrong answer for the user’s requested state even though its UUID is valid. The component contract does not prove intent.

## 3. current coverage audit

### backend and persistence coverage

`libraries/python/phoebe_v3_agent/middleware/dynamic_ui_test.py` covers valid receipts, CSV and JSONL, generic tables, domain suggestions, context entity inference, invalid UUIDs, non-standard JSON, missing columns, strict types, output size, row count, and column limits. It does not check whether the rows answer a natural-language question.

`libraries/python/phoebe_v3_agent/tools/interaction/render_test.py:75-926` covers file and inline mode, query normalization, domain components, agency rules, context receipts, outreach and recommendation references, invalid payloads, unsafe paths, action validation, title validation, missing sources, and unavailable sandboxes. These are unit tests with mocked services. They do not run the V3 agent, query, or browser.

`libraries/python/phoebe_agent_render_artifacts/byte_store_test.py:13-79` covers private S3 upload and retention after verification failure. It does not exercise a full event-to-API-to-browser read.

`services/api/routes/organizations/phoebe_agent_runs/workspace_files_test.py:296-1263` covers run and mode scoping, UI-context binding, selected row verification, snapshot reads after workspace overwrite, retry behavior, CSV, pagination, authorization, row and response caps, and recycled workspaces. This is strong API coverage. It does not grade model component choice or content semantics.

`services/api/routes/organizations/phoebe_agent_runs/workspace_file_reading_test.py:30-361` covers lease waits, Modal failures, output validation, and authorization errors. It does not load a browser.

Event and bundle tests cover the persister wiring. Relevant anchors include `libraries/python/agent_event_framework/tool_execution_test.py:1532`, `libraries/python/agent_event_framework/run_loop_test.py:1522-1596`, and the internal-readonly and sidebar bundle tests. These tests prove metadata persistence wiring, not user-visible correctness.

### web coverage

`apps/web/lib/agent_render_artifact.test.ts` covers strict agency-rule receipt parsing and rejects wrong tool, wrong component, table paths, extra columns, and empty rows.

`apps/web/features/phoebe_chat_v3/registry/registry.test.tsx:2482-2565` covers selectable shifts and domain table projection. `:2755-2897` covers generated-table and agency-rule receipt attachment, raw-row exclusion, unsafe URL rejection, and typed presentation projection. The broad registry fixture tests at `:3569-3744` cover event shapes and unsafe sentinels.

`apps/web/features/phoebe_chat_v3/ui/primitives/primitives.test.tsx:215-300` covers hidden ID columns, human labels, client values, date formatting, status text, nulls, booleans, and list rendering. These tests use hand-built rows.

`apps/web/features/phoebe_chat_v3/ui/agency_rules_preview.test.tsx:44-135` covers safe rendering, malformed rows, error text, and citations. It does not compare source guidance meaning.

`apps/web/playwright/v3_event_rendering.spec.ts:307-462` covers replacement of a shifts table by an outreach card, typed selection, and sending. `:1204-1427` covers live outreach summaries, reload behavior, and outreach detail. These tests mock API responses. They do not execute a real model, S3 object, or workspace mutation.

The legacy `apps/web/components/phoebe_chat_view/dynamic_widgets/generic_table_adapter.test.ts` covers an older adapter. It is not coverage of the current V3 receipt path.

### eval coverage

The current `v3-evals/features/general_chat` feature has two cases. `v3-evals/features/general_chat/cases.py:166-410` grades “show my outreaches” and “schedule today.” The checks require names, dates, and exclusions in the final reply. They accept a badge token as evidence. They do not require `render_dynamic_ui`, inspect a render receipt, inspect snapshot bytes, or open the frontend.

`v3-evals/src/execute/hosted.py:40-180` captures native traces and tool evidence. `v3-evals/src/extract/evidence.py:33-163` exposes tool calls and persisted transcript events, but the general-chat feature only extracts text and persisted records. A UI case could extend this extraction seam.

The newer native subject protocol supports structured V3 runs. `evals/subject_execution_protocol/__init__.py:739-770` defines seeded multi-turn V3 inputs. `:1468-1525` and `:1659-1700` define step and final observations with tool calls and tool results. `evals/runners/seeded_org/seeded_org_v3_chat_runner.py:261-520` runs the real conversation and collects those observations. This is a good lower-level hook, but no current case grades generated UI output.

PR #17174 established the current feature-case pattern: setup, user action, evidence extraction, named deterministic checks, and separate failure diagnosis. PR #17024 added reproducible fixtures, exact measurements, code-aware tool checks, and separate diagnostic grading. The `v3-evals/README.md:57-71` and `:173-174` document the same philosophy: golden cases should preserve known bugs, missing evidence is an infrastructure error, and tests should target the running agent path.

## 4. candidate eval designs

### design a: full-stack golden rendered-result cases

This should be the first implementation.

**What it checks.** Each case asks for one high-signal result, such as today’s open shifts, caregivers matching a role and location, one client’s details, or current agency rules. The case checks:

- the agent calls `render_dynamic_ui` when the case requires a visual result;
- the component matches the intended surface;
- the receipt has the expected format, row count, columns, title, and source event;
- snapshot bytes can be read and match the receipt hash and size;
- rows contain the expected IDs and values, with no decoys;
- ordering, time window, aggregation, and selected columns match the case;
- selection actions are absent or present only when the case requires them.

**Hook layer.** Add a feature under `v3-evals/features/` and reuse the existing setup, hosted execution, trace capture, and cleanup. Extend `extract` to read render tool results and, for each receipt, fetch the bound workspace-file API pages before cleanup. If the native subject path becomes the standard runner, use `SeededOrgV3ChatObservation` as the equivalent source.

**Case definition.** Keep setup data in `dataset.json`. Keep expected IDs, row predicates, component, and semantic requirements in `rubric.json`. Use a small Python case class for seed setup, extraction, and named checks. Include decoys and boundary dates. Use frozen or relative dates so the case stays valid.

**Grading.** Use deterministic checks for receipt fields, parsed rows, IDs, counts, date ranges, and exact values. Add a separate diagnostic judge only after a deterministic failure. Do not use an LLM judge for facts that the seeded data can prove.

**Cost and complexity.** Low to medium implementation cost. Each live trial costs about $0.20-$1 for the agent under the current V3 eval guidance. A failed trial can add about $0.50-$0.75 for diagnosis. Start with three to five cases and one or three trials per case.

### design b: artifact and transport fault matrix

This design targets pipeline breakage without spending model tokens.

**What it checks.** Feed the API and frontend saved event fixtures for missing files, invalid rows, stale workspace content, S3 missing objects, hash mismatch, wrong event IDs, mode mismatch, pagination limits, wide columns, empty rows, and frontend receipt drift. Assert exact status codes, error text, and safe fallback behavior.

**Hook layer.** Add API tests around `render_artifact_reading.py`, `workspace_files.py`, and `ui_context_validation.py`. Add browser tests that mount the actual V3 table resource against a mocked route. Use a local byte-store fake or test S3 seam. Keep the saved event and artifact bytes together as a golden fixture.

**Case definition.** Use typed fixture objects with a receipt, event, metadata row, byte content, and expected result. Include one fixture for a workspace overwrite after render. Include one fixture for a partial-page failure.

**Grading.** Deterministic only. Check status, response body, displayed failure text, pagination state, and absence of raw unsafe data. This design must never decide whether a table answers a question.

**Cost and complexity.** Low recurring cost and medium test setup. It is fast and stable. It is the right guard for the S3 snapshot seam, but it cannot find a model that chooses the wrong table.

### design c: browser usefulness and visual cases

This design checks the final coordinator experience for a small subset.

**What it checks.** Load real or replayed receipts in the V3 browser and check that the table shows the useful columns, readable title, date formatting, client pills, selection controls, pagination, agency-rule preview, and failure state. Include wide columns, long values, 15-plus rows, and a reload after workspace mutation.

**Hook layer.** Run the real V3 agent from design A, then open the saved run in Playwright. Pass the real event ID to the workspace-file route. Use DOM assertions for required content. Use screenshots only for layout regressions.

**Case definition.** Reuse the golden cases from design A. Add display expectations to the rubric, such as visible title, visible client names, hidden technical IDs, and visible next-page control. Keep visual cases few because layout assertions are more brittle.

**Grading.** Use deterministic DOM checks for text, roles, buttons, page transitions, and error states. Use a small LLM judge only for broad readability or whether the selected presentation is understandable, and keep that result diagnostic until human-validated. Do not let a judge override exact data checks.

**Cost and complexity.** Medium to high. It needs a healthy web stack, browser orchestration, and stable fixture loading. It adds little model cost if it replays a saved run, but it adds CI time and screenshot maintenance.

## 5. recommended first slice

Start with design A and four cases:

1. Today’s open shifts: require the `shifts` component, exact local-day filtering, readable time columns, and no tomorrow decoy.
2. Matching caregivers: require a generic or caregiver result with exact role and location filters, no ineligible decoys, and useful columns.
3. Client detail: require one `client_detail` row with the requested client and no unrelated rows.
4. Agency rules: require `agency_rules`, concise rows, and source-meaning checks against seeded playbook text.

Add design B before adding browser grading. Add one design C browser case after the artifact contract is stable.

The first golden cases should record the failure they are meant to prevent. A passing case must prove the rendered artifact and its rows. A prose answer alone must fail when the case asks for a result card.

## 6. open questions for henry

- Is the first family limited to `render_dynamic_ui` tables and domain cards, or should chart attachments be included now?
- Which questions should require a visual result? Some questions may reasonably pass with prose.
- Should expected rows use exact golden bytes, exact IDs and values, or semantic invariants such as filters and ordering?
- Should generic-table versus domain-component choice be a hard deterministic rule for known question types?
- Should empty results render a successful empty state instead of failing the tool?
- Is the browser DOM part of the pass criterion, or is the persisted receipt and snapshot enough for the first phase?
- Which execution path is the long-term hook: the hosted `v3-evals` harness or the newer native subject protocol?
- What model, trial count, and cost limit should gate these cases in CI?
- Should selection actions be graded in the first slice, including the distinction between useful actions and distracting actions?
- Which layer owns schema drift between Python receipt contracts and TypeScript parsers?
- Does the team want a cleanup job or metric for S3 objects uploaded before a failed event transaction?
