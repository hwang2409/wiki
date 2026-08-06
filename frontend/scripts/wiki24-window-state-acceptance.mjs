import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "./wiki32-harness.mjs";

const ROUNDTRIP_SCREENSHOT = "/tmp/wiki-24-window-state-roundtrip.png";
const COLLAPSE_SCREENSHOT = "/tmp/wiki-24-window-state-collapsed.png";
const WINDOW_0 = "WIKI-320";
const WINDOW_1 = "WIKI-321";
const SUBAGENT_ID = "abcdef12";
const TARGETED = process.argv.includes("--targeted");
const OUT_PATH = TARGETED
  ? "/tmp/wiki-24-window-state-targeted.json"
  : "/tmp/wiki-24-window-state-acceptance.json";

function claudeUser(text, timestamp) {
  return {
    type: "user",
    timestamp,
    message: { content: text },
  };
}

function claudeAssistant(blocks, timestamp) {
  return {
    type: "assistant",
    timestamp,
    message: { content: blocks },
  };
}

function textBlock(text) {
  return { type: "text", text };
}

function thinkingBlock(thinking) {
  return { type: "thinking", thinking };
}

function toolUseBlock(id, name, input) {
  return { type: "tool_use", id, name, input };
}

function toolResultBlock(toolUseId, text, isError = false) {
  return {
    type: "tool_result",
    tool_use_id: toolUseId,
    is_error: isError,
    content: [{ type: "text", text }],
  };
}

function writeJsonl(path, rows) {
  writeFileSync(path, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

function appendJsonl(path, rows) {
  for (const row of rows) appendFileSync(path, `${JSON.stringify(row)}\n`);
}

function writeAgentRegistry(registryPath, tickets) {
  const spawnedAt = "2026-07-09T00:00:00Z";
  const workers = Object.fromEntries(
    tickets.map(([ticket, transcript]) => [
      ticket,
      {
        current: {
          window: "@9999",
          spawned_at: spawnedAt,
          transcript,
          kind: "cc",
          role: "worker",
        },
      },
    ]),
  );
  const orchestrators = Object.fromEntries(
    tickets.map(([ticket, transcript]) => [
      ticket,
      {
        window: "@9999",
        spawned_at: spawnedAt,
        transcript,
      },
    ]),
  );
  writeFileSync(
    registryPath,
    JSON.stringify(
      {
        ...workers,
        _orchestrators: orchestrators,
      },
      null,
      2,
    ),
  );
}

function transcriptTimestamp(index) {
  return `2026-07-09T00:${String(index % 60).padStart(2, "0")}:${String((index * 7) % 60).padStart(2, "0")}Z`;
}

function buildTranscript(ticket, { includeActivity = false } = {}) {
  const rows = [claudeUser(`${ticket} warmup`, transcriptTimestamp(0))];
  for (let index = 1; index <= 80; index += 1) {
    if (includeActivity && index === 20) {
      rows.push(
        claudeAssistant(
          [
            thinkingBlock("working through the next state transition"),
            toolUseBlock("call-1", "exec_command", { cmd: `echo ${ticket}` }),
            toolResultBlock("call-1", `${ticket}\nexited with code 0`),
          ],
          transcriptTimestamp(index),
        ),
      );
      continue;
    }
    rows.push(
      claudeAssistant(
        [
          textBlock(
            Array.from(
              { length: 6 },
              (_, line) => `${ticket} row ${index} line ${line} ${"scroll body ".repeat(6)}`,
            ).join("\n"),
          ),
        ],
        transcriptTimestamp(index),
      ),
    );
  }
  return rows;
}

function buildSubagentTranscript() {
  return [
    claudeUser("inspect the regression surface", transcriptTimestamp(81)),
    claudeAssistant(
      [textBlock("subagent transcript body\n".repeat(24))],
      transcriptTimestamp(82),
    ),
  ];
}

function buildLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${WINDOW_0}` },
      },
      {
        id: "window-1",
        focusedPaneId: "pane-2",
        layout: { kind: "pane", id: "pane-2", path: `agent://${WINDOW_1}` },
      },
      {
        id: "window-2",
        focusedPaneId: "pane-3",
        layout: {
          kind: "split",
          direction: "row",
          ratio: 0.5,
          first: { kind: "pane", id: "pane-3", path: "agent://WIKI-322" },
          second: { kind: "pane", id: "pane-4", path: "agent://WIKI-323" },
        },
      },
    ],
  };
}

async function openAgentPage(page, baseUrl, layout, ticket) {
  await page.addInitScript(({ storedLayout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { storedLayout: layout });
  await page.goto(`${baseUrl}/#/agent/${ticket}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".agent-session-surface-main .session-scroll");
}

async function leaderChord(page, key) {
  await page.keyboard.press("Control+a");
  await page.keyboard.press(key);
}

async function activeWindowIndex(page) {
  return page.locator(".tmux-status-item.is-active .tmux-status-index").textContent();
}

async function focusedPaneKey(page) {
  return page.locator(".pane-frame.is-focused").evaluate((node) => node.getAttribute("data-pane-key"));
}

async function activeMainTicket(page) {
  return page.locator(".agent-session-surface > .session-header .session-ticket").first().textContent();
}

async function scrollState(page) {
  return page.locator(".agent-session-surface-main .session-scroll").evaluate((node) => ({
    scrollTop: node.scrollTop,
    scrollHeight: node.scrollHeight,
    clientHeight: node.clientHeight,
  }));
}

async function mainScrollAnchor(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector(".agent-session-surface-main .session-scroll");
    if (!(scroll instanceof HTMLElement)) return null;
    const scrollRect = scroll.getBoundingClientRect();
    const rows = [...document.querySelectorAll(".agent-session-surface-main .session-virtual-row")];
    const row = rows.find((candidate) => candidate.getBoundingClientRect().bottom > scrollRect.top + 1);
    if (!(row instanceof HTMLElement)) return null;
    const key = Number(row.dataset.groupKey ?? "");
    const top = Number(row.dataset.rowTop ?? "");
    if (!Number.isFinite(key) || !Number.isFinite(top)) return null;
    return {
      key,
      top,
      offset: scroll.scrollTop - top,
      scrollTop: scroll.scrollTop,
    };
  });
}

async function debugScrollState(page) {
  const [scroll, anchor] = await Promise.all([scrollState(page), mainScrollAnchor(page)]);
  return { ...scroll, anchor };
}

async function pickWindowChooserTicket(page, ticket) {
  await leaderChord(page, "w");
  await page.waitForSelector(".fleet-switcher");
  await page.evaluate((targetTicket) => {
    const button = [...document.querySelectorAll(".fleet-switcher-result")].find((node) => {
      if (!(node instanceof HTMLButtonElement) || node.disabled) return false;
      const label = node.querySelector(".quick-switcher-name")?.textContent?.trim();
      return label === targetTicket;
    });
    if (!(button instanceof HTMLButtonElement)) {
      throw new Error(`Missing chooser target for ${targetTicket}`);
    }
    button.click();
  }, ticket);
}

async function runChooserDraftCheck(page, draftText) {
  const composer = page.locator(".session-composer textarea");
  await page.waitForFunction(
    (expected) => document.querySelector(".session-composer textarea")?.value === expected,
    draftText,
  );
  await pickWindowChooserTicket(page, WINDOW_1);
  await page.waitForFunction(
    (current) => {
      const ticket = document.querySelector(".agent-session-surface > .session-header .session-ticket")?.textContent;
      return Boolean(ticket) && ticket !== current;
    },
    WINDOW_0,
  );
  const chooserTicketDuringMove = await activeMainTicket(page);
  const chooserDraftDuringMove = await composer.inputValue();
  await pickWindowChooserTicket(page, WINDOW_0);
  await page.waitForFunction(
    (expected) => document.querySelector(".agent-session-surface > .session-header .session-ticket")?.textContent === expected,
    WINDOW_0,
  );
  await page.waitForFunction(
    (expected) => document.querySelector(".session-composer textarea")?.value === expected,
    draftText,
  );
  return {
    intermediateTicket: chooserTicketDuringMove,
    intermediateDraft: chooserDraftDuringMove,
    restoredTicket: await activeMainTicket(page),
    restoredDraftExact: (await composer.inputValue()) === draftText,
  };
}

async function settleVirtualizer(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => resolve(null));
        });
      }),
  );
  await page.waitForTimeout(150);
}

async function waitForStableMainScroll(page, timeoutMs = 4000) {
  const start = Date.now();
  let previous = await scrollState(page);
  let stableSince = Date.now();
  while (Date.now() - start < timeoutMs) {
    await settleVirtualizer(page);
    const next = await scrollState(page);
    const unchanged =
      Math.abs(next.scrollTop - previous.scrollTop) <= 1 &&
      Math.abs(next.scrollHeight - previous.scrollHeight) <= 1;
    if (unchanged) {
      if (Date.now() - stableSince >= 150) return next;
    } else {
      stableSince = Date.now();
    }
    previous = next;
  }
  return previous;
}

async function main() {
  const fixtures = makeFixtureRoot("wiki24-window-state-");
  process.env.WIKI_UI_STATE_PATH = join(fixtures.root, "ui-state.json");

  const transcriptPaths = new Map();
  for (const ticket of [WINDOW_0, WINDOW_1, "WIKI-322", "WIKI-323"]) {
    const transcript = join(fixtures.root, `${ticket}.jsonl`);
    writeJsonl(transcript, buildTranscript(ticket, { includeActivity: ticket === WINDOW_0 }));
    transcriptPaths.set(ticket, transcript);
  }

  const subagentsDir = join(fixtures.root, WINDOW_0, "subagents");
  mkdirSync(subagentsDir, { recursive: true });
  writeJsonl(join(subagentsDir, `agent-${SUBAGENT_ID}.jsonl`), buildSubagentTranscript());

  writeAgentRegistry(
    fixtures.registryPath,
    [...transcriptPaths.entries()].map(([ticket, transcript]) => [ticket, transcript]),
  );
  writeQueue(fixtures.queuePath, WINDOW_0);

  const layout = buildLayout();
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
  const consoleErrors = [];
  const pageErrors = [];
  const sessionRequests = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });
  page.on("response", (response) => {
    if (!response.url().includes(`/api/agents/${WINDOW_0}/session`)) return;
    sessionRequests.push({ url: response.url(), ts: Date.now() });
  });

  const transcript0 = transcriptPaths.get(WINDOW_0);
  const draftText = ["alpha", "bravo", "charlie", "delta", "echo"].join("\n");
  const appendTimers = [];

  try {
    await openAgentPage(page, backend.baseUrl, layout, WINDOW_0);
    await page.waitForSelector(".session-subagent-chip");

    const composer = page.locator(".session-composer textarea");
    await composer.fill(draftText);

    await page.locator(".session-subagent-chip").click();
    await page.waitForSelector(".session-side-panel-subagent");

    const resizeHandle = page.locator(".session-side-panel-subagent .session-resize");
    const resizeBox = await resizeHandle.boundingBox();
    if (!resizeBox) throw new Error("Missing resize handle");
    await page.mouse.move(resizeBox.x + resizeBox.width / 2, resizeBox.y + resizeBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(resizeBox.x - 140, resizeBox.y + resizeBox.height / 2, { steps: 12 });
    await page.mouse.up();
    const panelWidthBefore = await page.locator(".session-side-panel-subagent").evaluate((node) => node.clientWidth);

    await page.locator(".agent-session-surface-main .session-scroll").evaluate((node) => {
      node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 1500);
      node.dispatchEvent(new Event("scroll"));
    });
    const scrollBeforeState = await waitForStableMainScroll(page);
    const scrollTopBefore = scrollBeforeState.scrollTop;
    const anchorBefore = await mainScrollAnchor(page);
    const savedBeforeSwitch = await debugScrollState(page);

    const awayStart = Date.now();
    appendTimers.push(
      setTimeout(() => {
        appendJsonl(transcript0, [
          claudeAssistant([textBlock("catch-up one\n".repeat(8))], transcriptTimestamp(90)),
        ]);
      }, 2500),
    );
    appendTimers.push(
      setTimeout(() => {
        appendJsonl(transcript0, [
          claudeAssistant([textBlock("catch-up two\n".repeat(8))], transcriptTimestamp(91)),
        ]);
      }, 5500),
    );
    await leaderChord(page, "1");
    await page.waitForFunction(
      () => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "1",
    );
    await page.waitForTimeout(10000);

    const returnStart = Date.now();
    await leaderChord(page, "0");
    await page.waitForFunction(
      (expected) => document.querySelector(".session-composer textarea")?.value === expected,
      draftText,
    );
    await page.waitForSelector(".session-side-panel-subagent");
    await page.waitForTimeout(1200);
    const roundTripScroll = await waitForStableMainScroll(page).catch(async (error) => {
      const debug = await debugScrollState(page);
      console.error(JSON.stringify({ scrollTopBefore, anchorBefore, savedBeforeSwitch, debug }, null, 2));
      throw error;
    });
    const anchorAfter = await mainScrollAnchor(page);
    const anchorMatches =
      anchorBefore &&
      anchorAfter &&
      anchorBefore.key === anchorAfter.key &&
      Math.abs(anchorBefore.offset - anchorAfter.offset) <= 24;
    if (!anchorMatches) {
      const debug = await debugScrollState(page);
      console.error(JSON.stringify({
        scrollTopBefore,
        anchorBefore,
        anchorAfter,
        savedBeforeSwitch,
        roundTripScroll,
        debug,
      }, null, 2));
      throw new Error(`Restored anchor mismatch key=${anchorAfter?.key ?? "null"} offsetDelta=${anchorBefore && anchorAfter ? Math.abs(anchorBefore.offset - anchorAfter.offset) : "n/a"}`);
    }
    const roundTripDraftExact = (await composer.inputValue()) === draftText;
    const panelWidthAfter = await page.locator(".session-side-panel-subagent").evaluate((node) => node.clientWidth);
    const hiddenWindowRequests = sessionRequests.filter(
      (entry) => entry.ts >= awayStart && entry.ts < returnStart,
    ).length;
    const returnRequests = sessionRequests.filter(
      (entry) => entry.ts >= returnStart && entry.ts < returnStart + 1200,
    ).length;

    await page.screenshot({ path: ROUNDTRIP_SCREENSHOT, fullPage: true });

    if (TARGETED) {
      const chooserDraft = await runChooserDraftCheck(page, draftText);
      const targetedResult = {
        fixtureRoot: fixtures.root,
        fixtures: {
          registry: fixtures.registryPath,
          queue: fixtures.queuePath,
          statusDir: fixtures.statusDir,
          transcripts: Object.fromEntries(transcriptPaths),
          subagentTranscript: join(subagentsDir, `agent-${SUBAGENT_ID}.jsonl`),
        },
        screenshots: {
          roundTrip: ROUNDTRIP_SCREENSHOT,
        },
        consoleErrors,
        pageErrors,
        roundTrip: {
          draftExact: roundTripDraftExact,
          hiddenWindowRequests,
          returnRequests,
          panelWidthBefore,
          panelWidthAfter,
          panelWidthDelta: Math.abs(panelWidthAfter - panelWidthBefore),
          scrollTopBefore,
          scrollTopAfter: roundTripScroll.scrollTop,
          scrollDelta: Math.abs(roundTripScroll.scrollTop - scrollTopBefore),
          anchorBefore,
          anchorAfter,
          anchorOffsetDelta: anchorBefore && anchorAfter ? Math.abs(anchorAfter.offset - anchorBefore.offset) : null,
        },
        chooserDraft,
        pass: {
          noConsoleErrors: consoleErrors.length === 0 && pageErrors.length === 0,
          roundTrip:
            hiddenWindowRequests === 0 &&
            returnRequests === 1 &&
            Math.abs(panelWidthAfter - panelWidthBefore) <= 8 &&
            Boolean(anchorMatches) &&
            roundTripDraftExact,
          chooserDraft:
            chooserDraft.intermediateTicket !== WINDOW_0 &&
            chooserDraft.intermediateDraft !== draftText &&
            chooserDraft.restoredTicket === WINDOW_0 &&
            chooserDraft.restoredDraftExact,
        },
      };
      writeFileSync(OUT_PATH, JSON.stringify(targetedResult, null, 2));
      console.log(JSON.stringify(targetedResult, null, 2));
      return;
    }

    await openAgentPage(page, backend.baseUrl, layout, WINDOW_0);
    await page.waitForSelector(".session-subagent-chip");
    await page.locator(".agent-session-surface-main .session-scroll").evaluate((node) => {
      node.scrollTop = node.scrollHeight;
      node.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(150);
    const pinnedAwayStart = Date.now();
    appendTimers.push(
      setTimeout(() => {
        appendJsonl(transcript0, [
          claudeAssistant([textBlock("pinned catch-up\n".repeat(6))], transcriptTimestamp(92)),
        ]);
      }, 2500),
    );
    await leaderChord(page, "1");
    await page.waitForFunction(
      () => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "1",
    );
    await page.waitForTimeout(10000);
    const pinnedReturnStart = Date.now();
    await leaderChord(page, "0");
    await page.waitForSelector(".agent-session-surface-main .session-scroll");
    await page.waitForTimeout(1200);
    const pinnedScroll = await scrollState(page);

    await page.locator(".agent-session-surface-main .session-scroll").evaluate((node) => {
      node.scrollTop = 2600;
      node.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(200);
    // WIKI-244: the activity trace is always visible — there is no collapse
    // state to persist. Assert the body survives a window round-trip instead.
    await page.waitForSelector(".session-activity .session-activity-body");
    await leaderChord(page, "1");
    await page.waitForFunction(
      () => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "1",
    );
    await page.waitForTimeout(1200);
    await leaderChord(page, "0");
    await page.waitForFunction(
      () => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "0",
    );
    const collapsedAfterReturn = (await page.locator(".session-activity .session-activity-body").count()) > 0;
    await page.screenshot({ path: COLLAPSE_SCREENSHOT, fullPage: true });

    const smoke = {};
    await leaderChord(page, "l");
    smoke.l = (await activeWindowIndex(page)) === "1";
    await leaderChord(page, "h");
    smoke.h = (await activeWindowIndex(page)) === "0";
    await leaderChord(page, "1");
    smoke.digit1 = (await activeWindowIndex(page)) === "1";
    await leaderChord(page, "0");
    smoke.digit0 = (await activeWindowIndex(page)) === "0";
    await leaderChord(page, "2");
    smoke.digit2 = (await activeWindowIndex(page)) === "2";
    const focusedBefore = await focusedPaneKey(page);
    await leaderChord(page, "j");
    const focusedAfterJ = await focusedPaneKey(page);
    smoke.j = focusedBefore !== focusedAfterJ;
    await leaderChord(page, "k");
    smoke.k = (await focusedPaneKey(page)) === focusedBefore;
    const paneCountBeforeZoom = await page.locator(".workspace-panes .pane-frame").count();
    await leaderChord(page, "z");
    smoke.zOn = (await page.locator(".workspace-panes .pane-frame").count()) === 1;
    await leaderChord(page, "z");
    smoke.zOff = (await page.locator(".workspace-panes .pane-frame").count()) === paneCountBeforeZoom;
    await leaderChord(page, "w");
    await page.waitForSelector(".fleet-switcher");
    smoke.w = true;
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    const windowCountBeforeClose = await page.locator(".tmux-status-item").count();
    await leaderChord(page, "x");
    await page.waitForFunction(
      (expected) => document.querySelectorAll(".tmux-status-item").length === expected,
      windowCountBeforeClose + 1,
    );
    smoke.x = (await page.locator(".tmux-status-item").count()) === windowCountBeforeClose + 1;

    await openAgentPage(page, backend.baseUrl, layout, WINDOW_0);
    const chooserComposer = page.locator(".session-composer textarea");
    await chooserComposer.fill(draftText);
    const chooserDraft = await runChooserDraftCheck(page, draftText);

    const pinnedDistance = Math.abs(
      pinnedScroll.scrollHeight - pinnedScroll.scrollTop - pinnedScroll.clientHeight,
    );
    const result = {
      fixtureRoot: fixtures.root,
      fixtures: {
        registry: fixtures.registryPath,
        queue: fixtures.queuePath,
        statusDir: fixtures.statusDir,
        transcripts: Object.fromEntries(transcriptPaths),
        subagentTranscript: join(subagentsDir, `agent-${SUBAGENT_ID}.jsonl`),
      },
      screenshots: {
        roundTrip: ROUNDTRIP_SCREENSHOT,
        collapsed: COLLAPSE_SCREENSHOT,
      },
      consoleErrors,
      pageErrors,
      roundTrip: {
        draftExact: roundTripDraftExact,
        hiddenWindowRequests,
        returnRequests,
        panelWidthBefore,
        panelWidthAfter,
        panelWidthDelta: Math.abs(panelWidthAfter - panelWidthBefore),
        scrollTopBefore,
        scrollTopAfter: roundTripScroll.scrollTop,
        scrollDelta: Math.abs(roundTripScroll.scrollTop - scrollTopBefore),
        anchorBefore,
        anchorAfter,
        anchorOffsetDelta: anchorBefore && anchorAfter ? Math.abs(anchorAfter.offset - anchorBefore.offset) : null,
      },
      chooserDraft,
      pinnedRoundTrip: {
        hiddenWindowRequests: sessionRequests.filter(
          (entry) => entry.ts >= pinnedAwayStart && entry.ts < pinnedReturnStart,
        ).length,
        returnRequests: sessionRequests.filter(
          (entry) => entry.ts >= pinnedReturnStart && entry.ts < pinnedReturnStart + 1200,
        ).length,
        pinnedDistance,
      },
      collapse: {
        restoredCollapsed: collapsedAfterReturn,
      },
      smoke,
      pass: {
        noConsoleErrors: consoleErrors.length === 0 && pageErrors.length === 0,
        roundTrip:
          hiddenWindowRequests === 0 &&
          returnRequests === 1 &&
          Math.abs(panelWidthAfter - panelWidthBefore) <= 8 &&
          Boolean(anchorMatches) &&
          roundTripDraftExact,
        chooserDraft:
          chooserDraft.intermediateTicket !== WINDOW_0 &&
          chooserDraft.intermediateDraft !== draftText &&
          chooserDraft.restoredTicket === WINDOW_0 &&
          chooserDraft.restoredDraftExact,
        pinned: pinnedDistance <= 24,
        collapsed: collapsedAfterReturn,
        smoke: Object.values(smoke).every(Boolean),
      },
    };

    writeFileSync(OUT_PATH, JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    appendTimers.forEach((timer) => clearTimeout(timer));
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
