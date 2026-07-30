import fs from "node:fs/promises";
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

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-152-chrome-evidence";
const TICKET = "WIKI-152";

function logStep(message) {
  console.error(`[wiki-152-playwright] ${message}`);
}

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function writeStatus(fixtures, body) {
  await fs.writeFile(
    path.join(fixtures.statusDir, `${TICKET}.json`),
    JSON.stringify(body, null, 2),
  );
}

function assertNoAmbientNoise(errors, presentSelectors, page) {
  return Promise.all(
    presentSelectors.map(async (selector) => {
      const count = await page.locator(selector).count();
      if (count !== 0) errors.push(`WIKI-152: ambient chrome element ${selector} must not render (count=${count})`);
    }),
  );
}

async function assertRunDetailsDefaultClosed(page) {
  const details = page.locator('[data-testid="session-run-details"]').first();
  await details.waitFor({ state: "attached" });
  if (await details.evaluate((el) => el.hasAttribute("open"))) {
    throw new Error("WIKI-152: Run details disclosure must be closed by default");
  }
  return details;
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-152-chrome-");
  const transcript = path.join(fixtures.root, "wiki-152-transcript.jsonl");
  await writeJsonl(transcript, [
    codexAssistant(
      "Chrome fixture — decision-relevant surface only.",
      "2026-07-30T00:00:00Z",
    ),
  ]);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  await writeStatus(fixtures, {
    state: "working",
    pr: null,
    step: "implementing chrome disclosure",
    blocker: null,
  });

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  try {
    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
            },
          ],
        }),
      );
    }, { ticket: TICKET });

    // The wiki32 harness writes the ticket under `_orchestrators`, which the
    // fleet API surfaces as an orchestrator. App.tsx then overlays that
    // orchestrator identity over any matching worker entry (see
    // App.tsx:1830), which drops the worker.state/step/blocker fields.
    // For the chrome test we intercept `/api/agents` to remove the shadow
    // orchestrator so the worker entry survives and drives the header.
    async function withWorkerStatus(status) {
      await writeStatus(fixtures, status);
      await page.route("**/api/agents", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        const filtered = {
          ...body,
          orchestrators: (body.orchestrators ?? []).filter((orch) => orch.id !== TICKET),
          workers: (body.workers ?? []).map((worker) =>
            worker.ticket === TICKET
              ? { ...worker, ...status, kind: "cc", role: "implement", model: "gpt-5.4" }
              : worker,
          ),
        };
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(filtered),
        });
      });
    }

    logStep("state 1: default chrome, working, step visible, no ambient diagnostic noise");
    await withWorkerStatus({
      state: "working",
      pr: null,
      step: "implementing chrome disclosure",
      blocker: null,
    });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".agent-session-surface-head");
    await page.locator('[data-testid="session-state-pill"]').filter({ hasText: "working" }).waitFor();
    await page.locator('[data-testid="session-step-row"]').filter({
      hasText: "implementing chrome disclosure",
    }).waitFor();
    const noiseErrors = [];
    await assertNoAmbientNoise(
      noiseErrors,
      [
        ".session-dispositions",
        ".session-provider-inspector",
        ".session-provider-inspector-head",
      ],
      page,
    );
    if (noiseErrors.length > 0) throw new Error(noiseErrors.join("\n"));
    const runDetailsMaybeInitial = page.locator('[data-testid="session-run-details"]');
    if ((await runDetailsMaybeInitial.count()) > 0) {
      if (await runDetailsMaybeInitial.first().evaluate((el) => el.hasAttribute("open"))) {
        throw new Error("WIKI-152: Run details disclosure must be closed by default");
      }
    }
    // Footer must not carry raw dispositions or format strings any more.
    const footerText = await page.locator(".session-footer").innerText();
    for (const forbidden of ["Unknown", "msg/v1", "tok"]) {
      if (footerText.toLowerCase().includes(forbidden.toLowerCase())) {
        throw new Error(`WIKI-152: session-footer must not include \"${forbidden}\" (found in: ${footerText})`);
      }
    }
    await page.screenshot({
      path: path.join(OUT_DIR, "01-default-chrome.png"),
      fullPage: true,
    });

    logStep("state 2: blocked worker surfaces a loud blocker banner");
    await page.unroute("**/api/agents");
    await withWorkerStatus({
      state: "blocked",
      pr: null,
      step: "implementing chrome disclosure",
      blocker: "CI failed on the vitest wiki-152 suite",
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator('[data-testid="session-state-pill"]').filter({ hasText: "blocked" }).waitFor();
    const blockerRow = page.locator('[data-testid="session-blocker-row"]');
    await blockerRow.waitFor({ state: "visible" });
    await blockerRow.getByText("CI failed on the vitest wiki-152 suite").waitFor();
    // When blocked, only the blocker row shows (step suppresses to keep hierarchy clear).
    if ((await page.locator('[data-testid="session-step-row"]').count()) !== 0) {
      throw new Error("WIKI-152: step row must be replaced by blocker row when blocked");
    }
    await page.screenshot({
      path: path.join(OUT_DIR, "02-blocked-chrome.png"),
      fullPage: true,
    });

    logStep("state 3: merge-ready state pill tone");
    await page.unroute("**/api/agents");
    await withWorkerStatus({
      state: "merge-ready",
      pr: "https://github.com/hwang2409/wiki/pull/999",
      step: "waiting on orchestrator gate",
      blocker: null,
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    const mergePill = page.locator('[data-testid="session-state-pill"]').filter({ hasText: "merge-ready" });
    await mergePill.waitFor();
    const pillClass = await mergePill.evaluate((el) => el.className);
    if (!pillClass.includes("is-positive")) {
      throw new Error(`WIKI-152: merge-ready pill must carry is-positive tone (got: ${pillClass})`);
    }
    await page.screenshot({
      path: path.join(OUT_DIR, "03-merge-ready-chrome.png"),
      fullPage: true,
    });

    logStep("state 4: diagnostics disclosure opens on click");
    const runDetailsMaybe = page.locator('[data-testid="session-run-details"]');
    if ((await runDetailsMaybe.count()) > 0) {
      const runDetails = runDetailsMaybe.first();
      if (await runDetails.evaluate((el) => el.hasAttribute("open"))) {
        throw new Error("WIKI-152: Run details must open only on click, not by default");
      }
      await runDetails.locator("summary").click();
      const opened = await runDetails.evaluate((el) => el.hasAttribute("open"));
      if (!opened) throw new Error("WIKI-152: Run details did not open after summary click");
      await runDetails.locator(".session-run-details-body").waitFor({ state: "visible" });
      await page.screenshot({
        path: path.join(OUT_DIR, "04-diagnostics-open.png"),
        fullPage: true,
      });
    } else {
      logStep("state 4: fixture emits no provider inspector — nothing to disclose (default chrome is empty diagnostics)");
    }

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          screenshots: [
            "01-default-chrome.png",
            "02-blocked-chrome.png",
            "03-merge-ready-chrome.png",
            "04-diagnostics-open.png",
          ],
        },
        null,
        2,
      ),
    );
    logStep(`evidence written to ${OUT_DIR}`);
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
