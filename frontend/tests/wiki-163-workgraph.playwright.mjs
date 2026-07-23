import { spawnSync } from "node:child_process";
import { existsSync, unlinkSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = [
  process.env.WIKI_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
].find((candidate) => candidate && existsSync(candidate));
const LIVE_TICKET = "WIKI-901";
const ARCHIVED_TICKET = "WIKI-902";
const SCREENSHOTS = {
  live: "/tmp/wiki-163-workgraph-live.png",
  replay: "/tmp/wiki-163-workgraph-replay.png",
  snapshot: "/tmp/wiki-163-workgraph-snapshot.png",
};

function logStep(message) {
  console.error(`[wiki-163-playwright] ${message}`);
}

function finding() {
  return {
    id: "F-blk001",
    severity: "BLOCKING",
    title: "stale cache read on session merge",
    file: "frontend/src/transcript-merge.ts",
    line: 42,
    observed: "merge reuses rows after invalidation",
    why_wrong: "dropped rows resurface in the transcript",
    do_instead: "bust the merge cache on every echo",
    source_worker: `${LIVE_TICKET}-REVIEW1`,
    source_sha: "abc1234",
    resolved_by: null,
    created_at: "2026-07-22T12:00:00Z",
  };
}

function graphAppend(fixtures, ticket, edgeKind, from, to, extraArgs = []) {
  if (!PYTHON) throw new Error("No Python runtime found for the workgraph fixture");
  const payloadPath = path.join(fixtures.root, `payload-${ticket}-${edgeKind}-${from}-${to}.json`);
  const result = spawnSync(
    PYTHON,
    [
      path.join(ROOT, "wiki"),
      "graph",
      "append",
      ticket,
      "--edge-kind",
      edgeKind,
      "--from",
      from,
      "--to",
      to,
      "--payload-file",
      payloadPath,
      ...extraArgs,
    ],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        WIKI_AGENT_STATUS_DIR: fixtures.statusDir,
        WIKI_WORKGRAPH_SNAPSHOT_DIR: fixtures.workgraphsDir,
        WIKI_AGENT_REGISTRY_PATH: fixtures.registryPath,
      },
      input: "",
      encoding: "utf8",
    }
  );
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout;
}

async function writePayload(fixtures, ticket, edgeKind, from, to, payload) {
  const payloadPath = path.join(fixtures.root, `payload-${ticket}-${edgeKind}-${from}-${to}.json`);
  await fs.writeFile(payloadPath, JSON.stringify(payload, null, 2));
}

async function appendEdge(fixtures, ticket, edgeKind, from, to, payload, extraArgs = []) {
  await writePayload(fixtures, ticket, edgeKind, from, to, payload);
  return graphAppend(fixtures, ticket, edgeKind, from, to, extraArgs);
}

function spawnPayload(ticket, role, model) {
  return {
    ticket,
    role,
    model,
    effort: "high",
    worktree: `/tmp/${ticket}-wt`,
    request_id: `req-${ticket}-${role}`,
  };
}

async function buildLiveWorkgraph(fixtures) {
  await appendEdge(fixtures, LIVE_TICKET, "spawn", "N-1", "N-2", spawnPayload(LIVE_TICKET, "implement", "gpt-5.6-luna"), ["--orch", "wiki"]);
  await appendEdge(fixtures, LIVE_TICKET, "spawn", "N-1", "N-3", spawnPayload(`${LIVE_TICKET}-REVIEW1`, "review", "gpt-5.6-sol"));
  await appendEdge(fixtures, LIVE_TICKET, "verdict", "N-3", "N-1", {
    worker: `${LIVE_TICKET}-REVIEW1`,
    sha: "abc1234",
    state: "NOT-MERGE-READY",
    findings: [finding()],
    created_at: "2026-07-22T12:00:00Z",
  });
  await appendEdge(fixtures, LIVE_TICKET, "steer", "N-1", "N-2", {
    target_worker: LIVE_TICKET,
    mode: "now",
    findings: [finding()],
    created_at: "2026-07-22T12:01:00Z",
  });
}

async function buildArchivedSnapshot(fixtures) {
  await appendEdge(fixtures, ARCHIVED_TICKET, "spawn", "N-1", "N-2", spawnPayload(ARCHIVED_TICKET, "implement", "gpt-5.6-luna"), ["--orch", "wiki"]);
  await appendEdge(fixtures, ARCHIVED_TICKET, "verdict", "N-2", "N-1", {
    worker: ARCHIVED_TICKET,
    sha: "def5678",
    state: "MERGE-READY",
    findings: [],
    created_at: "2026-07-22T13:00:00Z",
  });
  await appendEdge(fixtures, ARCHIVED_TICKET, "archive", "N-1", "N-2", {
    outcome: "merged",
    ended_at: "2026-07-22T13:30:00Z",
  });
  // Archived ticket: hot /tmp copy is gone, only the ~/.wiki snapshot remains.
  unlinkSync(path.join(fixtures.statusDir, `${ARCHIVED_TICKET}.workgraph.json`));
}

async function writeTranscript(fixtures, ticket) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: `wiki-163-${ticket}` },
    {
      type: "assistant",
      timestamp: "2026-07-22T11:59:00Z",
      message: { role: "assistant", content: [{ type: "text", text: "working the ticket" }] },
    },
  ];
  const transcript = path.join(fixtures.root, `wiki-163-${ticket}.jsonl`);
  await fs.writeFile(transcript, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
  return transcript;
}

function sessionLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${LIVE_TICKET}` },
      },
    ],
  };
}

async function expectText(locator, expected, label) {
  const deadline = Date.now() + 5000;
  let text = "";
  while (Date.now() < deadline) {
    text = (await locator.textContent().catch(() => ""))?.trim() ?? "";
    if (text.includes(expected)) return;
    await new Promise((resolveSleep) => setTimeout(resolveSleep, 100));
  }
  throw new Error(`${label}: expected "${expected}", got "${text}"`);
}

async function main() {
  logStep("building live + archived workgraph fixtures through the real `wiki graph append` writer");
  const fixtures = makeFixtureRoot("wiki-163-workgraph-");
  await buildLiveWorkgraph(fixtures);
  await buildArchivedSnapshot(fixtures);
  const transcript = await writeTranscript(fixtures, LIVE_TICKET);
  writeRegistry(fixtures.registryPath, [[LIVE_TICKET, transcript]]);
  writeQueue(fixtures.queuePath, LIVE_TICKET, []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error(`[wiki-163-playwright] page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") console.error(`[wiki-163-playwright] console: ${message.text()}`);
  });

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-dark");
    }, { layout: sessionLayout() });

    logStep("live view: spec-canonical route #/agents/<TICKET>/graph");
    await page.goto(`${backend.baseUrl}/#/agents/${LIVE_TICKET}/graph`, { waitUntil: "domcontentloaded" });
    const panel = page.locator(".session-side-panel-graph");
    await panel.waitFor({ state: "visible" });
    await panel.locator(".workgraph-dag").waitFor({ state: "visible" });

    const nodeCount = await panel.locator(".workgraph-node").count();
    if (nodeCount !== 3) throw new Error(`live DAG expected 3 nodes, got ${nodeCount}`);
    if ((await panel.locator(".workgraph-node.is-orchestrator").count()) !== 1) {
      throw new Error("live DAG missing the orchestrator node");
    }
    for (const kind of ["spawn", "verdict", "steer"]) {
      if ((await panel.locator(`.workgraph-edge.is-${kind}`).count()) === 0) {
        throw new Error(`live DAG missing a ${kind} edge`);
      }
    }

    await expectText(panel.locator(".workgraph-source"), "live file", "live source pill");
    const health = panel.locator(".workgraph-health");
    await health.waitFor({ state: "visible" });
    await expectText(health.locator(".workgraph-health-cell.is-state"), "iterating", "health state");
    await expectText(health.locator(".workgraph-health-cell.is-alarm .workgraph-health-value"), "1", "blocking count");

    const findingRows = panel.locator(".workgraph-finding-row");
    if ((await findingRows.count()) !== 1) throw new Error("live findings table expected 1 open finding");
    await expectText(findingRows.first().locator(".workgraph-severity"), "blocking", "finding severity");
    await expectText(findingRows.first(), "stale cache read", "finding title");
    await findingRows.first().click();
    await panel.locator(".workgraph-finding-detail").waitFor({ state: "visible" });
    await expectText(panel.locator(".workgraph-finding-detail"), "bust the merge cache", "finding detail");
    await panel.screenshot({ path: SCREENSHOTS.live });

    logStep("replay view: scrub frames chronologically");
    await panel.getByRole("tab", { name: "replay" }).click();
    await panel.locator(".workgraph-scrubber").waitFor({ state: "visible" });
    await expectText(panel.locator(".workgraph-frame-info"), "edge 4/4", "replay opens on the final frame");
    if ((await panel.locator(".workgraph-tick").count()) !== 4) throw new Error("replay expected 4 edge ticks");

    await panel.locator(".workgraph-tick").first().click();
    await expectText(panel.locator(".workgraph-frame-info"), "edge 1/4", "tick click scrubs to frame 1");
    await expectText(panel.locator(".workgraph-frame-info"), "spawn N-1→N-2", "frame 1 is the first spawn edge");
    const frameOneNodes = await panel.locator(".workgraph-node").count();
    if (frameOneNodes !== 2) throw new Error(`frame 1 expected 2 nodes present, got ${frameOneNodes}`);
    await panel.locator(".workgraph-findings-empty").waitFor({ state: "visible" });

    await panel.locator(".workgraph-step").nth(1).click(); // next-edge step
    await expectText(panel.locator(".workgraph-frame-info"), "edge 2/4", "step advances one frame");
    const frameTwoNodes = await panel.locator(".workgraph-node").count();
    if (frameTwoNodes !== 3) throw new Error(`frame 2 expected 3 nodes present, got ${frameTwoNodes}`);

    await panel.locator(".workgraph-tick").nth(2).click(); // verdict frame
    await expectText(panel.locator(".workgraph-frame-info"), "edge 3/4", "verdict frame selected");
    if ((await panel.locator(".workgraph-finding-row").count()) !== 1) {
      throw new Error("verdict frame should surface its finding in the table");
    }
    if ((await panel.locator(".workgraph-edge.is-current").count()) !== 1) {
      throw new Error("replay should highlight the current edge");
    }
    await panel.screenshot({ path: SCREENSHOTS.replay });

    logStep("archived ticket: renders from ~/.wiki snapshot after the hot copy is gone");
    await page.goto(`${backend.baseUrl}/#/agents/${ARCHIVED_TICKET}/graph`, { waitUntil: "domcontentloaded" });
    const archivedPanel = page.locator(".session-side-panel-graph");
    await archivedPanel.waitFor({ state: "visible" });
    await archivedPanel.locator(".workgraph-dag").waitFor({ state: "visible" });
    await expectText(archivedPanel.locator(".workgraph-source"), "snapshot", "archived source pill");
    await expectText(
      archivedPanel.locator(".workgraph-health-cell.is-state"),
      "archived",
      "archived health state"
    );
    if ((await archivedPanel.locator(".workgraph-node.is-archived").count()) !== 1) {
      throw new Error("archived DAG should mark the archived worker node");
    }

    await archivedPanel.getByRole("tab", { name: "replay" }).click();
    await expectText(archivedPanel.locator(".workgraph-frame-info"), "edge 3/3", "snapshot replay works");
    await archivedPanel.locator(".workgraph-tick").first().click();
    await expectText(archivedPanel.locator(".workgraph-frame-info"), "edge 1/3", "snapshot replay scrubs");
    await archivedPanel.screenshot({ path: SCREENSHOTS.snapshot });

    await fs.writeFile(
      "/tmp/wiki-163-fixtures.json",
      JSON.stringify({ root: fixtures.root, screenshots: SCREENSHOTS }, null, 2)
    );
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("workgraph live + replay + snapshot verification complete");
}

await main();
