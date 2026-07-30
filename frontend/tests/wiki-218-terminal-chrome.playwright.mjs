import { rmSync, writeFileSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-218-terminal-chrome-evidence";
const TICKET = "WIKI-370";

// Keep shell startup deterministic in CI-like runs.
process.env.SHELL = "/bin/bash";

await fs.mkdir(OUT_DIR, { recursive: true });

function logStep(message) {
  console.error(`[wiki-218] ${message}`);
}

async function leader(page, key) {
  await page.keyboard.press("Control+a");
  await page.keyboard.press(key);
}

function writeJsonl(filePath, rows) {
  writeFileSync(filePath, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

const fixtures = makeFixtureRoot("wiki-218-chrome-");
const transcriptPath = path.join(fixtures.root, `${TICKET}.jsonl`);
writeJsonl(transcriptPath, [
  {
    type: "session_meta",
    timestamp: "2026-07-09T00:00:00Z",
    payload: { id: "sess-wiki-218", cwd: process.cwd(), model: "gpt-5" },
  },
  codexUser("terminal chrome fixture", "2026-07-09T00:00:01Z"),
  codexAssistant("terminal chrome fixture body", "2026-07-09T00:00:02Z"),
]);
writeFileSync(
  fixtures.registryPath,
  JSON.stringify(
    {
      _orchestrators: {
        [TICKET]: { window: "@9999", spawned_at: "2026-07-09T00:00:00Z", transcript: transcriptPath },
      },
    },
    null,
    2
  )
);
writeFileSync(fixtures.queuePath, "{}\n");

const result = {
  colsAfterResize: null,
  colsBeforeResize: null,
  detailsRows: null,
  detailsHiddenByDefault: null,
  endedStatusWordVisible: null,
  endedTitle: null,
  legacyChipCount: null,
  pageErrors: [],
  renamePersisted: null,
  renamedTitle: null,
  statusLabel: null,
  findHeaderFits: null,
  sizeFlashSeen: null,
  title: null,
  viewportBackground: null,
};

let backend = null;
let browser = null;

try {
  logStep("starting isolated backend");
  backend = await startBackend(fixtures);
  browser = await chromium.launch({
    headless: true,
    args: ["--use-angle=swiftshader-webgl", "--enable-webgl"],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("pageerror", (error) => result.pageErrors.push(`page:${error.message}`));

  await page.addInitScript(() => {
    localStorage.setItem(
      "wiki-window-layout-v2",
      JSON.stringify({
        version: 2,
        activeWindowId: "window-0",
        windows: [
          {
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-370" },
          },
        ],
      })
    );
    localStorage.setItem("wiki-sidebar-visible", "false");
  });

  logStep("opening app and creating a terminal pane");
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".agent-session-surface-main .session-scroll");
  await page.keyboard.press("Control+a");
  await page.keyboard.press("t");
  await page.waitForFunction(() => Object.keys(window.__wikiTerminals ?? {}).length > 0);
  const terminalId = await page.evaluate(() => Object.keys(window.__wikiTerminals)[0]);
  await page.waitForFunction(
    (id) => window.__wikiTerminals?.[id]?.status?.() === "live",
    terminalId
  );

  logStep("checking friendly title and default-quiet header");
  await page.waitForFunction(() => {
    const text = document.querySelector(".terminal-pane-title")?.textContent ?? "";
    return text.length > 0 && !text.startsWith("terminal://");
  });
  result.title = await page.evaluate(
    () => document.querySelector(".terminal-pane-title")?.textContent ?? null
  );
  result.legacyChipCount = await page.locator(".terminal-pane-chip").count();
  result.detailsHiddenByDefault = (await page.locator(".terminal-pane-details").count()) === 0;

  logStep("checking the letterbox fix");
  result.viewportBackground = await page.evaluate(() => {
    const viewport = document.querySelector(".terminal-pane-host .xterm-viewport");
    return viewport ? getComputedStyle(viewport).backgroundColor : null;
  });

  logStep("opening the details disclosure");
  await page.getByRole("button", { name: "Terminal details" }).click();
  await page.waitForSelector(".terminal-pane-details");
  result.detailsRows = await page.evaluate(() =>
    [...document.querySelectorAll(".terminal-pane-details-label")].map(
      (node) => node.textContent?.trim() ?? ""
    )
  );

  logStep("renaming the terminal from the details disclosure");
  await page.getByRole("button", { name: "rename" }).click();
  await page.waitForSelector(".terminal-pane-rename-input");
  await page.fill(".terminal-pane-rename-input", "deploy shell");
  await page.keyboard.press("Enter");
  await page.waitForFunction(
    () => document.querySelector(".terminal-pane-title")?.textContent === "deploy shell"
  );
  result.renamedTitle = await page.evaluate(
    () => document.querySelector(".terminal-pane-title")?.textContent ?? null
  );
  result.renamePersisted = await page.evaluate(
    (id) => localStorage.getItem(`wiki-terminal-name:${id}`),
    terminalId
  );

  logStep("checking the find overlay at narrow pane width");
  await page.evaluate(() => {
    const split = document.querySelector(".pane-split.row");
    if (!(split instanceof HTMLElement)) throw new Error("Pane split not found");
    split.style.setProperty("--split-ratio", "0.88");
  });
  await page.waitForFunction(
    () => document.querySelector(".terminal-pane")?.getBoundingClientRect().width <= 220
  );
  await page.evaluate((id) => window.__wikiTerminals?.[id]?.terminal.focus(), terminalId);
  await page.waitForFunction(
    (id) =>
      document.activeElement instanceof HTMLTextAreaElement &&
      document.activeElement.dataset.terminalInput === "true" &&
      document.activeElement.dataset.terminalId === id,
    terminalId
  );
  await page.keyboard.press("Control+f");
  await page.waitForSelector(".terminal-pane-find");
  result.findHeaderFits = await page.evaluate(() => {
    const header = document.querySelector(".terminal-pane-header");
    const find = document.querySelector(".terminal-pane-find");
    return Boolean(
      header &&
        find &&
        header.scrollWidth <= header.clientWidth &&
        find.getBoundingClientRect().right <= header.getBoundingClientRect().right
    );
  });
  await page.getByRole("button", { name: "Close find" }).click();

  logStep("checking split-drag reflow and the transient size flash");
  result.colsBeforeResize = await page.evaluate(
    (id) => window.__wikiTerminals?.[id]?.terminal.cols ?? null,
    terminalId
  );
  await page.evaluate(() => {
    const split = document.querySelector(".pane-split.row");
    if (!(split instanceof HTMLElement)) throw new Error("Pane split not found");
    split.style.setProperty("--split-ratio", "0.72");
  });
  await page.waitForSelector(".terminal-pane-size-flash", { timeout: 4000 });
  result.sizeFlashSeen = true;
  await page.waitForFunction(
    ({ id, before }) => (window.__wikiTerminals?.[id]?.terminal.cols ?? before) !== before,
    { id: terminalId, before: result.colsBeforeResize }
  );
  result.colsAfterResize = await page.evaluate(
    (id) => window.__wikiTerminals?.[id]?.terminal.cols ?? null,
    terminalId
  );
  await page.waitForFunction(() => !document.querySelector(".terminal-pane-size-flash"));

  logStep("checking the reactive status label");
  await leader(page, "k");
  await page.waitForFunction(() => document.querySelector(".pane-frame.is-focused .agent-session-surface") !== null);
  await leader(page, "x");
  await page.waitForFunction(() => document.querySelectorAll(".terminal-pane").length === 1);
  result.statusLabel = await page.evaluate(
    () => [...document.querySelectorAll(".tmux-status-label")]
      .map((node) => node.textContent?.trim() ?? "")
      .find((label) => label === "deploy shell") ?? null
  );

  logStep("checking the ended state keeps the friendly title");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".terminal-pane-notice");
  result.endedTitle = await page.evaluate(
    () => document.querySelector(".terminal-pane-title")?.textContent ?? null
  );
  result.endedStatusWordVisible =
    (await page.locator(".terminal-pane-status-word.is-ended").count()) === 1;

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-218-ended.png") });

  if (!result.title || /^terminal:\/\//.test(result.title) || /^[0-9a-f-]{36}$/.test(result.title)) {
    throw new Error(`Expected a friendly terminal title, saw ${result.title}`);
  }
  if (result.legacyChipCount !== 0) {
    throw new Error(`Expected no legacy chips, saw ${result.legacyChipCount}`);
  }
  if (!result.detailsHiddenByDefault) {
    throw new Error("Details disclosure should be hidden by default");
  }
  if (result.viewportBackground !== "rgba(0, 0, 0, 0)") {
    throw new Error(`Expected transparent xterm viewport, saw ${result.viewportBackground}`);
  }
  for (const label of ["session", "renderer", "shell", "cwd"]) {
    if (!result.detailsRows?.includes(label)) {
      throw new Error(`Details disclosure is missing "${label}": ${JSON.stringify(result.detailsRows)}`);
    }
  }
  if (result.renamedTitle !== "deploy shell" || result.renamePersisted !== "deploy shell") {
    throw new Error(
      `Rename did not stick: title=${result.renamedTitle} stored=${result.renamePersisted}`
    );
  }
  if (!result.findHeaderFits) {
    throw new Error("Find overlay overflowed the narrow terminal header");
  }
  if (result.statusLabel?.trim() !== "deploy shell") {
    throw new Error(`Status label did not follow rename: ${result.statusLabel}`);
  }
  if (!result.sizeFlashSeen) {
    throw new Error("Size flash did not appear during split resize");
  }
  if (
    !result.colsBeforeResize ||
    !result.colsAfterResize ||
    result.colsAfterResize === result.colsBeforeResize
  ) {
    throw new Error(
      `Terminal did not reflow on split resize: ${result.colsBeforeResize} -> ${result.colsAfterResize}`
    );
  }
  if (result.endedTitle !== "deploy shell") {
    throw new Error(`Ended pane lost its title: ${result.endedTitle}`);
  }
  if (!result.endedStatusWordVisible) {
    throw new Error("Ended state did not surface a status word");
  }
  if (result.pageErrors.length > 0) {
    throw new Error(result.pageErrors.join("\n"));
  }
} finally {
  logStep("shutting down");
  if (browser) await browser.close().catch(() => {});
  if (backend) await backend.stop().catch(() => {});
  rmSync(fixtures.root, { force: true, recursive: true });
}

console.log(JSON.stringify(result, null, 2));
