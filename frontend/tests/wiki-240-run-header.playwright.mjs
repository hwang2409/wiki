import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const UPDATE_EVIDENCE = process.env.WIKI_UPDATE_EVIDENCE === "1";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  || (UPDATE_EVIDENCE
    ? path.join(HERE, "evidence", "wiki-240")
    : await fs.mkdtemp(path.join(os.tmpdir(), "wiki-240-evidence-")));
const TICKET = "WIKI-240";

async function writeJsonl(target, rows) {
  await fs.writeFile(target, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}

async function writeStatus(fixtures, body) {
  await fs.writeFile(
    path.join(fixtures.statusDir, `${TICKET}.json`),
    JSON.stringify(body, null, 2),
  );
}

function loopState(round, cap = 8, danger = "normal") {
  return {
    round,
    cap,
    danger,
    unrouted_verdict_count: 2,
    plateau_length: 3,
    latest_verdict: null,
    latest_verdict_finding: "The header hid the current run step.",
    history: [
      {
        round: Math.max(1, round - 1),
        reviewer: `${TICKET}-REVIEW1`,
        spawned_at: "2026-08-02T12:00:00Z",
        verdict_state: "NOT-MERGE-READY",
        verdict_at: "2026-08-02T12:10:00Z",
        routed_at: "2026-08-02T12:12:00Z",
        archived_at: null,
        top_finding: "The header hid the current run step.",
        finding_signature: "header step hidden",
      },
    ],
  };
}

async function resizeAgentPane(page, label, targetRatio) {
  const split = page.locator(".pane-split.row");
  const divider = split.locator(".pane-divider.row");
  const bounds = await split.boundingBox();
  if (!bounds) throw new Error(`${label}: split pane has no bounds`);
  await divider.hover();
  await page.mouse.down();
  await page.mouse.move(
    bounds.x + bounds.width * targetRatio,
    bounds.y + bounds.height / 2,
    { steps: 8 },
  );
  await page.mouse.up();
  await page.waitForFunction(({ expected }) => {
    const pane = document.querySelector(".pane-frame[data-pane-key='pane-1']");
    if (!(pane instanceof HTMLElement)) return false;
    const splitRoot = pane.closest(".pane-split");
    if (!(splitRoot instanceof HTMLElement)) return false;
    const ratio = pane.getBoundingClientRect().width / splitRoot.getBoundingClientRect().width;
    return Math.abs(ratio - expected) <= 0.02;
  }, { expected: targetRatio });
}

async function assertPromptDockFitsPane(page, label) {
  const result = await page.locator(".pane-frame[data-pane-key='pane-1']").evaluate((pane) => {
    const paneRect = pane.getBoundingClientRect();
    const selectors = [".session-composer", ".session-footer"];
    return selectors.map((selector) => {
      const element = pane.querySelector(selector);
      if (!(element instanceof HTMLElement)) return { selector, missing: true };
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        selector,
        missing: false,
        visible:
          rect.width > 0
          && rect.height > 0
          && style.display !== "none"
          && style.visibility !== "hidden"
          && style.opacity !== "0",
        inside:
          rect.left >= paneRect.left
          && rect.right <= paneRect.right
          && rect.top >= paneRect.top
          && rect.bottom <= paneRect.bottom,
        overflow: element.scrollWidth > element.clientWidth + 1,
        bounds: {
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          top: Math.round(rect.top),
          bottom: Math.round(rect.bottom),
        },
        pane: {
          left: Math.round(paneRect.left),
          right: Math.round(paneRect.right),
          top: Math.round(paneRect.top),
          bottom: Math.round(paneRect.bottom),
        },
      };
    });
  });
  const failures = result.filter((entry) =>
    entry.missing || !entry.visible || !entry.inside || entry.overflow);
  if (failures.length > 0) {
    throw new Error(`${label}: prompt dock escaped its pane ${JSON.stringify(failures)}`);
  }
}

async function assertControlFitsVisiblePane(control, label) {
  const result = await control.evaluate((element) => {
    const pane = element.closest(".pane-frame[data-pane-key='pane-1']");
    const panel = element.closest(".session-side-panel");
    if (!(pane instanceof HTMLElement) || !(panel instanceof HTMLElement)) {
      return { missingSurface: true };
    }
    const rect = element.getBoundingClientRect();
    const paneRect = pane.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const visibleRect = {
      left: Math.max(paneRect.left, panelRect.left, 0),
      right: Math.min(paneRect.right, panelRect.right, window.innerWidth),
      top: Math.max(paneRect.top, panelRect.top, 0),
      bottom: Math.min(paneRect.bottom, panelRect.bottom, window.innerHeight),
    };
    const style = getComputedStyle(element);
    const hit = document.elementFromPoint(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2,
    );
    return {
      missingSurface: false,
      visible:
        rect.width > 0
        && rect.height > 0
        && style.display !== "none"
        && style.visibility !== "hidden"
        && style.opacity !== "0",
      inside:
        rect.left >= visibleRect.left
        && rect.right <= visibleRect.right
        && rect.top >= visibleRect.top
        && rect.bottom <= visibleRect.bottom,
      hittable: hit === element || element.contains(hit) || Boolean(hit && hit.contains(element)),
      hit: hit
        ? {
            tag: hit.tagName,
            className: typeof hit.className === "string" ? hit.className : "",
          }
        : null,
      overflow: element.scrollWidth > element.clientWidth + 1,
      control: {
        left: Math.round(rect.left),
        right: Math.round(rect.right),
        top: Math.round(rect.top),
        bottom: Math.round(rect.bottom),
      },
      visibleSurface: {
        left: Math.round(visibleRect.left),
        right: Math.round(visibleRect.right),
        top: Math.round(visibleRect.top),
        bottom: Math.round(visibleRect.bottom),
      },
    };
  });
  if (
    result.missingSurface
    || !result.visible
    || !result.inside
    || !result.hittable
    || result.overflow
  ) {
    throw new Error(`${label}: useful panel control is not usable ${JSON.stringify(result)}`);
  }
}

async function assertLoopDetailFitsPane(detail, label) {
  const result = await detail.evaluate((dialog) => {
    const pane = dialog.closest(".pane-frame[data-pane-key='pane-1']");
    if (!(pane instanceof HTMLElement)) return { missingPane: true };
    const paneRect = pane.getBoundingClientRect();
    const dock = pane.querySelector(".session-composer");
    const footer = pane.querySelector(".session-footer");
    if (!(dock instanceof HTMLElement) || !(footer instanceof HTMLElement)) {
      return { missingPane: false, missingPromptDock: true };
    }
    const dockRect = dock instanceof HTMLElement ? dock.getBoundingClientRect() : null;
    const footerRect = footer.getBoundingClientRect();
    const dockStyle = getComputedStyle(dock);
    const footerStyle = getComputedStyle(footer);
    const dialogRect = dialog.getBoundingClientRect();
    const elements = [dialog, ...dialog.querySelectorAll("*")]
      .filter((element) => element instanceof HTMLElement);
    const escaped = elements
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          name: element.className || element.tagName.toLowerCase(),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          top: Math.round(rect.top),
          bottom: Math.round(rect.bottom),
        };
      })
      .filter((rect) =>
        rect.left < Math.floor(paneRect.left)
        || rect.right > Math.ceil(paneRect.right));
    const horizontalOverflow = elements
      .filter((element) => element.scrollWidth > element.clientWidth + 1)
      .map((element) => ({
        name: element.className || element.tagName.toLowerCase(),
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));
    return {
      missingPane: false,
      missingPromptDock: false,
      dialog: {
        left: Math.round(dialogRect.left),
        right: Math.round(dialogRect.right),
        bottom: Math.round(dialogRect.bottom),
        width: Math.round(dialogRect.width),
      },
      pane: {
        left: Math.round(paneRect.left),
        right: Math.round(paneRect.right),
        width: Math.round(paneRect.width),
      },
      dialogEscaped:
        dialogRect.left < paneRect.left
        || dialogRect.right > paneRect.right
        || dialogRect.top < paneRect.top
        || dialogRect.bottom > paneRect.bottom,
      dockTop: dockRect ? Math.round(dockRect.top) : null,
      promptDockVisible:
        dockRect.width > 0
        && dockRect.height > 0
        && footerRect.width > 0
        && footerRect.height > 0
        && dockStyle.display !== "none"
        && dockStyle.visibility !== "hidden"
        && dockStyle.opacity !== "0"
        && footerStyle.display !== "none"
        && footerStyle.visibility !== "hidden"
        && footerStyle.opacity !== "0",
      escaped,
      horizontalOverflow,
      overlapsDock: dockRect ? dialogRect.bottom > dockRect.top + 1 : false,
    };
  });
  if (
    result.missingPane
    || result.missingPromptDock
    || !result.promptDockVisible
    || result.dialogEscaped
    || result.escaped?.length
    || result.horizontalOverflow?.length
    || result.overlapsDock
  ) {
    throw new Error(`${label}: loop detail escaped its pane ${JSON.stringify(result)}`);
  }
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-240-header-");
  await fs.mkdir(path.join(fixtures.root, "vault"), { recursive: true });
  await fs.writeFile(
    path.join(fixtures.root, "vault", "split-fixture.md"),
    "# split fixture\n\nThis pane keeps the wide-window split open.\n",
  );
  const transcript = path.join(fixtures.root, "wiki-240-transcript.jsonl");
  await writeJsonl(transcript, [
    codexAssistant(
      "Stable header fixture. All run functions remain available.",
      "2026-08-02T12:00:00Z",
    ),
  ]);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  let currentStatus = {
    state: "working",
    pr: "https://github.com/hwang2409/wiki/pull/999",
    step: "building the two-level run header and checking responsive action access",
    blocker: null,
  };
  let currentLoop = loopState(3);
  await writeStatus(fixtures, currentStatus);

  const backend = await startBackend(fixtures);
  let browser = null;
  let page = null;
  try {
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    let replaceRequests = 0;
    const replayRun = {
      run_id: "fixture-replay-run",
      agent_id: TICKET,
      orch_id: "wiki",
      role: "implement",
      provider: "codex",
      model: "gpt-5.6-sol",
      outcome: "merge-ready",
      state: "completed",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:15:00Z",
      total_events: 2,
      initial_prompt_excerpt: "Rebuild the run header hierarchy.",
    };
    const workgraph = () => ({
      ticket: TICKET,
      orch: "wiki",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:12:00Z",
      nodes: [
        { id: "wiki", kind: "orchestrator", label: "wiki", state: "working" },
        { id: TICKET, kind: "implement", label: TICKET, state: currentStatus.state },
      ],
      edges: [
        {
          kind: "verdict",
          from: TICKET,
          to: "wiki",
          created_at: "2026-08-02T12:12:00Z",
          active: true,
          payload: {
            state: "NOT-MERGE-READY",
            findings: [{
              id: "R4-1",
              severity: "MEDIUM",
              title: "Keep useful actions visible in split panes",
              observed: "The split pane can hide panel controls.",
              why_wrong: "Hidden controls block required review work.",
              do_instead: "Keep each useful control inside the visible pane.",
              source_worker: `${TICKET}-REVIEW4`,
              source_sha: "01b6f098b272be526a4456e95e1fbf32ffcbf199",
              resolved_by: null,
            }],
          },
        },
      ],
      composite_health: {
        state: "iterating",
        open_findings: 1,
        blocking: 0,
        slowest_node_stall_seconds: 0,
        iteration_count: currentLoop.round,
      },
    });

    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      const agentPane = { kind: "pane", id: "pane-1", path: `agent://${ticket}` };
      const layout = window.innerWidth === 1280
        ? {
            kind: "split",
            direction: "row",
            ratio: 0.5,
            first: agentPane,
            second: { kind: "pane", id: "pane-2", path: "split-fixture.md" },
          }
        : agentPane;
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout,
            },
          ],
        }),
      );
    }, { ticket: TICKET });

    await page.route("**/api/models", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          models: [
            {
              id: "gpt-5.6-sol",
              label: "GPT-5.6 Sol",
              kind: "cdx",
              provider: "codex",
              supports_reasoning_effort: true,
              default_worker: true,
              default_orchestrator: false,
            },
            {
              id: "gpt-5.6-terra",
              label: "GPT-5.6 Terra",
              kind: "cdx",
              provider: "codex",
              supports_reasoning_effort: true,
              default_worker: false,
              default_orchestrator: false,
            },
          ],
        }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/replace`, async (route) => {
      replaceRequests += 1;
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "replace is disabled in this visual fixture" }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/pr`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          url: "https://github.com/hwang2409/wiki/pull/173",
          repo: "hwang2409/wiki",
          title: "Fixture PR ready for review",
          state: "OPEN",
          mergeable: "MERGEABLE",
          mergeStateStatus: "CLEAN",
          reviewDecision: "REVIEW_REQUIRED",
          headRefName: "wiki-240-run-header-hierarchy",
          additions: 24,
          deletions: 8,
          changedFiles: 2,
          statusChecks: [{
            name: "frontend gate",
            state: "pass",
            rawState: "SUCCESS",
            workflow: "test",
            detailsUrl: null,
          }],
          unresolvedThreads: [],
          diff: "diff --git a/header.tsx b/header.tsx\n--- a/header.tsx\n+++ b/header.tsx\n@@ -1 +1 @@\n-old header\n+two-level header\n",
        }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/workgraph/revisions`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { revision: 1, created_at_ns: 1_754_132_400_000_000_000, edge_count: 1 },
          { revision: 2, created_at_ns: 1_754_133_120_000_000_000, edge_count: 1 },
        ]),
      });
    });
    await page.route(`**/api/agents/${TICKET}/workgraph?revision=*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          source: "snapshot",
          revision: Number(new URL(route.request().url()).searchParams.get("revision")),
          workgraph: workgraph(),
          loop_state: currentLoop,
        }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/replay/runs`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ticket: TICKET, runs: [replayRun], runs_truncated: false }),
      });
    });
    await page.route("**/api/agent-runs/fixture-replay-run/replay/timeline?*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: replayRun,
          events: [
            {
              seq: 1,
              raw_seq: 1,
              ts: "2026-08-02T12:00:00Z",
              kind: "codex_assistant",
              disposition: "rendered",
              lifecycle_state: "working",
              summary: "Header fixture started.",
              bookmark: null,
            },
            {
              seq: 2,
              raw_seq: 2,
              ts: "2026-08-02T12:00:05Z",
              kind: "codex_assistant",
              disposition: "rendered",
              lifecycle_state: "merge-ready",
              summary: "Header fixture passed its focused checks.",
              bookmark: "verdict",
            },
          ],
          next_cursor: null,
          has_more: false,
          bookmarks: [{
            seq: 2,
            kind: "verdict",
            ts: "2026-08-02T12:00:05Z",
            summary: "Header fixture passed its focused checks.",
            event_kind: "codex_assistant",
          }],
          bookmarks_truncated: false,
          warnings: [],
        }),
      });
    });
    await page.route("**/api/agent-runs/fixture-replay-run/replay/events/*", async (route) => {
      const seq = Number(route.request().url().split("/").at(-1));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: replayRun.run_id,
          seq,
          raw: { seq, message: seq === 1 ? "fixture started" : "fixture passed" },
        }),
      });
    });

    await page.route("**/api/agents", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...body,
          orchestrators: (body.orchestrators ?? []).filter((orch) => orch.id !== TICKET),
          workers: (body.workers ?? []).map((worker) =>
            worker.ticket === TICKET
              ? {
                  ...worker,
                  ...currentStatus,
                  kind: "cdx",
                  role: "implement",
                  model: "gpt-5.6-sol",
                  run_id: "fixture-run",
                  canReview: true,
                  canReplace: true,
                }
              : worker,
          ),
        }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/workgraph`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          source: "live",
          workgraph: workgraph(),
          loop_state: currentLoop,
        }),
      });
    });
    await page.route(`**/api/autopilot/${TICKET}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          enabled: true,
          iteration_cap: 8,
          plateau_guard: 3,
          henry_ack_required_for_merge: false,
          last_action_at_ns: 1_754_000_000_000_000_000,
          halted: null,
          merge_ack_at_ns: null,
          actions: [
            {
              action: "steer-sent",
              at_ns: 1_754_000_000_000_000_000,
              source: "autopilot",
              preview: "keep the current step in the first reading level",
              target: TICKET,
            },
          ],
        }),
      });
    });

    const states = [
      {
        name: "working",
        status: {
          state: "working",
          pr: currentStatus.pr,
          step: "building the two-level run header and checking responsive action access",
          blocker: null,
        },
        loop: loopState(3),
      },
      {
        name: "merge-ready",
        status: {
          state: "merge-ready",
          pr: currentStatus.pr,
          step: "focused header tests pass twice; the screenshot audit is complete",
          blocker: null,
        },
        loop: loopState(6),
      },
      {
        name: "blocked",
        status: {
          state: "blocked",
          pr: currentStatus.pr,
          step: "waiting for the fixture service",
          blocker: "the fixture service stopped before the header audit completed",
        },
        loop: loopState(4),
      },
      {
        name: "at-cap",
        status: {
          state: "working",
          pr: currentStatus.pr,
          step: "review round eight is active at the configured loop cap",
          blocker: null,
        },
        loop: loopState(8),
      },
      {
        name: "over-cap",
        status: {
          state: "working",
          pr: currentStatus.pr,
          step: "review round nine continues after the configured loop cap",
          blocker: null,
        },
        loop: loopState(9),
      },
    ];

    for (const fixture of states) {
      currentStatus = fixture.status;
      currentLoop = fixture.loop;
      await writeStatus(fixtures, currentStatus);
      const viewports = [
        { name: "normal", width: 1440, height: 900, splitRatio: null },
        { name: "split-35", width: 1280, height: 900, splitRatio: 0.35 },
      ];
      if (fixture.name === "over-cap") {
        viewports.push({ name: "split-15", width: 1280, height: 900, splitRatio: 0.15 });
      }
      for (const viewport of viewports) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await page.goto("about:blank");
        await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
        const header = page.locator(".agent-session-surface-head");
        await header.waitFor({ state: "visible" });
        if (viewport.splitRatio !== null) {
          await resizeAgentPane(page, `${fixture.name}/${viewport.name}`, viewport.splitRatio);
          await assertPromptDockFitsPane(page, `${fixture.name}/${viewport.name}`);
        }
        await header.locator('[data-testid="session-state-pill"]').filter({ hasText: fixture.status.state }).waitFor();
        const expectedRound = fixture.loop.round > fixture.loop.cap
          ? `round ${fixture.loop.round} · cap ${fixture.loop.cap} exceeded`
          : `round ${fixture.loop.round} of ${fixture.loop.cap}`;
        await header.getByText(expectedRound, { exact: true }).waitFor();

        if ((await header.locator('[data-testid="session-header-primary"]').count()) !== 1
          || (await header.locator('[data-testid="session-header-secondary"]').count()) !== 1) {
          throw new Error(`${fixture.name}/${viewport.name}: header must have two levels`);
        }
        const actionLabels = await header.locator(".agent-surface-actions").innerText();
        for (const action of ["Replace", "Review", "Graph", "Replay"]) {
          if (!actionLabels.includes(action)) {
            throw new Error(`${fixture.name}/${viewport.name}: missing ${action} action`);
          }
        }
        if ((await header.getByRole("button", { name: "Close pane" }).count()) !== 1) {
          throw new Error(`${fixture.name}/${viewport.name}: close action is missing`);
        }
        if ((await header.getByRole("button", { name: /autopilot on/i }).count()) !== 1) {
          throw new Error(`${fixture.name}/${viewport.name}: autopilot control is missing`);
        }
        const undersizedTargets = await header.locator("button").evaluateAll((buttons) =>
          buttons
            .map((button) => ({
              name: button.getAttribute("aria-label") || button.textContent?.trim() || "button",
              height: Math.round(button.getBoundingClientRect().height),
            }))
            .filter((button) => button.height < 32),
        );
        if (undersizedTargets.length > 0) {
          throw new Error(`${fixture.name}/${viewport.name}: undersized targets ${JSON.stringify(undersizedTargets)}`);
        }
        const overflow = await header.evaluate((element) => element.scrollWidth > element.clientWidth);
        if (overflow) {
          const dimensions = await header.evaluate((element) => ({
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
            headerStyle: {
              alignItems: getComputedStyle(element).alignItems,
              flexDirection: getComputedStyle(element).flexDirection,
              flexWrap: getComputedStyle(element).flexWrap,
            },
            primaryStyle: {
              display: getComputedStyle(element.querySelector(".agent-session-head-primary")).display,
              width: getComputedStyle(element.querySelector(".agent-session-head-primary")).width,
            },
            overflowing: Array.from(element.querySelectorAll("*") )
              .filter((child) => child.scrollWidth > child.clientWidth)
              .map((child) => ({
                className: child.className,
                clientWidth: child.clientWidth,
                scrollWidth: child.scrollWidth,
              }))
              .slice(0, 8),
            outside: Array.from(element.querySelectorAll("*"))
              .map((child) => ({
                className: child.className,
                left: Math.round(child.getBoundingClientRect().left),
                right: Math.round(child.getBoundingClientRect().right),
              }))
              .filter((child) => child.right > Math.round(element.getBoundingClientRect().right))
              .slice(0, 8),
          }));
          throw new Error(`${fixture.name}/${viewport.name}: header clips horizontally ${JSON.stringify(dimensions)}`);
        }
        const escapedControls = await header.evaluate((root) => {
          const headerRect = root.getBoundingClientRect();
          return Array.from(root.querySelectorAll(
            ".loop-chrome-trigger, .loop-autopilot-toggle, .agent-surface-action, .session-close",
          ))
              .map((control) => {
                const rect = control.getBoundingClientRect();
                return {
                  name: control.getAttribute("aria-label") || control.textContent?.trim() || "control",
                  left: Math.round(rect.left),
                  right: Math.round(rect.right),
                };
              })
              .filter((control) => control.left < Math.floor(headerRect.left) || control.right > Math.ceil(headerRect.right));
        });
        if (escapedControls.length > 0) {
          throw new Error(`${fixture.name}/${viewport.name}: controls escaped header ${JSON.stringify(escapedControls)}`);
        }
        const clippedControls = await header.locator(
          ".loop-chrome-trigger, .loop-autopilot-toggle, .agent-surface-action, .session-close",
        ).evaluateAll((controls) => controls
          .filter((control) => control.scrollWidth > control.clientWidth + 1)
          .map((control) => ({
            name: control.getAttribute("aria-label") || control.textContent?.trim() || "control",
            clientWidth: control.clientWidth,
            scrollWidth: control.scrollWidth,
          })));
        if (clippedControls.length > 0) {
          throw new Error(`${fixture.name}/${viewport.name}: controls clipped content ${JSON.stringify(clippedControls)}`);
        }

        if (viewport.name !== "split-15") {
          await header.screenshot({
            path: path.join(OUT_DIR, `${fixture.name}-${viewport.name}.png`),
          });
        }

        if (fixture.name === "over-cap" && viewport.splitRatio !== null) {
          const loopTrigger = header.getByRole("button", { name: /show merge-ready loop history/i });
          await loopTrigger.click();
          const detail = header.locator(".loop-chrome-detail");
          await detail.getByText("latest finding").waitFor();
          await detail.getByText("The header hid the current run step.").first().waitFor();
          await detail.locator(".loop-history-row").waitFor();
          await detail.getByText("autopilot log").waitFor();
          await assertLoopDetailFitsPane(detail, `${fixture.name}/${viewport.name}`);
          const agentPane = page.locator(".pane-frame[data-pane-key='pane-1']");
          await agentPane.screenshot({
            path: path.join(OUT_DIR, `${fixture.name}-dialog-${viewport.name}.png`),
          });
          const autopilotLog = detail.locator(".loop-autopilot-log");
          await autopilotLog.scrollIntoViewIfNeeded();
          const logVisibility = await autopilotLog.evaluate((log) => {
            const dialog = log.closest(".loop-chrome-detail");
            if (!(dialog instanceof HTMLElement)) return false;
            const logRect = log.getBoundingClientRect();
            const dialogRect = dialog.getBoundingClientRect();
            return logRect.top >= dialogRect.top && logRect.bottom <= dialogRect.bottom;
          });
          if (!logVisibility) {
            throw new Error(`${fixture.name}/${viewport.name}: autopilot log is not reachable in loop detail`);
          }
          if (viewport.name === "split-15") {
            await agentPane.screenshot({
              path: path.join(OUT_DIR, `${fixture.name}-dialog-log-${viewport.name}.png`),
            });
          }
        }
      }
    }

    currentStatus = states[0].status;
    currentLoop = states[0].loop;
    await writeStatus(fixtures, currentStatus);
    const actionCases = ["Replace", "Review", "Graph", "Replay", "Close"];
    for (const split of [
      { name: "split-35", ratio: 0.35 },
      { name: "split-15", ratio: 0.15 },
    ]) {
      for (const action of actionCases) {
        const label = `action-${action.toLowerCase()}/${split.name}`;
        await page.setViewportSize({ width: 1280, height: 900 });
        await page.goto("about:blank");
        await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
        const header = page.locator(".agent-session-surface-head");
        await header.waitFor({ state: "visible" });
        await resizeAgentPane(page, label, split.ratio);
        await assertPromptDockFitsPane(page, label);
        await header.getByText("round 3 of 8", { exact: true }).waitFor();

        if (action === "Close") {
          await header.getByRole("button", { name: "Close pane" }).click();
          await page.locator(".pane-frame[data-pane-key='pane-1']").waitFor({ state: "detached" });
          const peerPane = page.locator(".pane-frame[data-pane-key='pane-2']");
          await peerPane.waitFor({ state: "visible" });
          await peerPane.getByText("This pane keeps the wide-window split open.", { exact: true }).waitFor();
          if ((await page.locator(".pane-split.row").count()) !== 0
            || (await peerPane.count()) !== 1
            || !(await peerPane.isVisible())) {
            throw new Error(`${label}: close action did not leave the peer pane`);
          }
          await page.screenshot({
            path: path.join(OUT_DIR, `action-${action.toLowerCase()}-${split.name}.png`),
          });
        } else {
          await header.getByRole("button", { name: action, exact: true }).click();
          if (action === "Replace") {
            const dialog = page.getByRole("dialog", { name: `Replace ${TICKET}` });
            await dialog.waitFor({ state: "visible" });
            const advanced = dialog.getByRole("button", { name: /Advanced/ });
            await advanced.focus();
            if (!(await advanced.evaluate((button) => document.activeElement === button))) {
              throw new Error(`${label}: Replace Advanced control did not receive focus`);
            }
            await advanced.click();
            const model = dialog.getByLabel("Model");
            const effort = dialog.getByLabel("Reasoning effort");
            await model.waitFor({ state: "visible" });
            await model.selectOption("gpt-5.6-terra");
            await effort.selectOption("medium");
            if ((await model.inputValue()) !== "gpt-5.6-terra"
              || (await effort.inputValue()) !== "medium") {
              throw new Error(`${label}: Replace selections did not update`);
            }
            const cancel = dialog.getByRole("button", { name: "Cancel", exact: true });
            await cancel.focus();
            if (!(await cancel.evaluate((button) => document.activeElement === button))) {
              throw new Error(`${label}: Replace Cancel control did not receive focus`);
            }
            await page.screenshot({
              path: path.join(OUT_DIR, `action-${action.toLowerCase()}-${split.name}.png`),
            });
            await cancel.click();
            await dialog.waitFor({ state: "detached" });
          } else {
            const panel = page.locator(`.session-side-panel-${action.toLowerCase()}`);
            await panel.waitFor({ state: "visible" });
            if (action === "Review") {
              const review = panel.locator(".pr-review");
              await review.getByText("Fixture PR ready for review", { exact: true }).waitFor();
              await review.getByText("frontend gate", { exact: true }).waitFor();
              await panel.getByText("Loading PR…", { exact: true }).waitFor({ state: "detached" });
              const refresh = review.getByRole("button", { name: "refresh", exact: true });
              await assertControlFitsVisiblePane(refresh, `${label}/refresh`);
              const refreshed = page.waitForResponse((response) =>
                response.url().endsWith(`/api/agents/${TICKET}/pr`) && response.ok());
              await refresh.click();
              await refreshed;
              await review.getByText("Fixture PR ready for review", { exact: true }).waitFor();
            } else if (action === "Graph") {
              const graph = panel.locator(".workgraph-panel");
              await graph.locator(".workgraph-dag").waitFor({ state: "visible" });
              await graph.getByText("Keep useful actions visible in split panes", { exact: true }).waitFor();
              await panel.getByText("Loading workgraph…", { exact: true }).waitFor({ state: "detached" });
              const replayTab = graph.getByRole("tab", { name: "replay", exact: true });
              await assertControlFitsVisiblePane(replayTab, `${label}/replay-tab`);
              const revisionLoaded = page.waitForResponse((response) =>
                response.url().includes(`/api/agents/${TICKET}/workgraph?revision=2`) && response.ok());
              await replayTab.click();
              await revisionLoaded;
              await graph.locator(".workgraph-scrubber").waitFor({ state: "visible" });
              await graph.getByText(/r2 of 2/).waitFor();
              await graph.locator(".workgraph-dag").waitFor({ state: "visible" });
              await graph.getByText("Loading revision r2…", { exact: true }).waitFor({ state: "detached" });
            } else if (action === "Replay") {
              const replay = panel.locator(".replay-panel");
              await replay.getByText("Header fixture started.", { exact: true }).waitFor();
              await replay.locator(".replay-event-raw").getByText("fixture started", { exact: false }).waitFor();
              await replay.getByText("loading timeline…", { exact: true }).waitFor({ state: "detached" });
              const next = replay.getByRole("button", { name: "Next event" });
              await assertControlFitsVisiblePane(next, `${label}/next-event`);
              const rawLoaded = page.waitForResponse((response) =>
                response.url().endsWith("/replay/events/2") && response.ok());
              await next.click();
              await rawLoaded;
              await replay.getByText("Header fixture passed its focused checks.", { exact: true }).waitFor();
              await replay.locator(".replay-event-raw").getByText("fixture passed", { exact: false }).waitFor();
            }
            await page.screenshot({
              path: path.join(OUT_DIR, `action-${action.toLowerCase()}-${split.name}.png`),
            });
            await panel.getByRole("button", { name: "Close" }).click();
            await panel.waitFor({ state: "detached" });
          }
        }
      }
    }

    if (replaceRequests !== 0) {
      throw new Error(`Replace test must remain non-destructive; saw ${replaceRequests} requests`);
    }

    if ((await page.locator('[data-testid="session-run-details"]').count()) !== 0) {
      throw new Error("WIKI-240: Run details must stay removed");
    }
    if ((await page.locator(".session-cost").count()) !== 0) {
      throw new Error("WIKI-240: cost chrome must stay removed");
    }

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify({
        screenshots: states.flatMap((fixture) => [
          `${fixture.name}-normal.png`,
          `${fixture.name}-split-35.png`,
        ]).concat([
          "over-cap-dialog-split-35.png",
          "over-cap-dialog-split-15.png",
          "over-cap-dialog-log-split-15.png",
          ...["split-35", "split-15"].flatMap((split) =>
            ["replace", "review", "graph", "replay", "close"].map((action) =>
              `action-${action}-${split}.png`)),
        ]),
        audit: [
          "two clear header levels at normal width and in real 35% and 15% split panes",
          "current step remains readable without horizontal clipping",
          "Replace, Review, Graph, Replay, Close, and autopilot remain discoverable",
          "Replace, Review, Graph, Replay, and Close complete safe interactions in isolated 35% and 15% split states",
          "Review, Graph, and Replay show final fixture content with useful controls inside the visible pane",
          "Close keeps the split fixture peer visible with its content intact",
          "the visible composer and footer stay inside every split pane",
          "open loop history, findings, plateau state, and autopilot log stay inside narrow panes",
          "dense controls keep at least 32px height and visible focus",
          "cost and Run details remain absent",
        ],
      }, null, 2),
    );
    console.error(`[wiki-240-playwright] evidence written to ${OUT_DIR}`);
  } finally {
    const cleanup = await Promise.allSettled([
      (async () => {
        try {
          if (page && !page.isClosed()) {
            try {
              await page.unrouteAll({ behavior: "ignoreErrors" });
            } finally {
              await page.close();
            }
          }
        } finally {
          await browser?.close();
        }
      })(),
      backend.stop(),
    ]);
    const errors = cleanup
      .filter((result) => result.status === "rejected")
      .map((result) => result.reason);
    if (errors.length > 0) throw new AggregateError(errors, "WIKI-240 fixture cleanup failed");
  }
}

await main();
