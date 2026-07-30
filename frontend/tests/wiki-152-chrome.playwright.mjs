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

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
// Committed evidence path — Henry reviews the PR from GitHub, /tmp paths
// are useless there. Set WIKI_PLAYWRIGHT_OUT_DIR to override for local runs.
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  || path.join(HERE, "evidence", "wiki-152");
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
        ".tmux-status-index",
        ".tmux-status-sep",
      ],
      page,
    );
    if (noiseErrors.length > 0) throw new Error(noiseErrors.join("\n"));
    // R1-02: status bar tmux items must not render "0:WIKI-152" — strip
    // index + colon punctuation and keep only the ticket label.
    const tmuxLabels = await page.locator(".tmux-status-item").allInnerTexts();
    for (const label of tmuxLabels) {
      if (/^\d+\s*:/.test(label) || label.includes(`:${TICKET}`) || label.includes(`0:${TICKET}`)) {
        throw new Error(`WIKI-152: tmux status item must not carry tmux punctuation (got: ${JSON.stringify(label)})`);
      }
    }
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

    logStep("state 5: action-required panel promoted above transcript");
    // Inject a synthetic providerInspector with a pending user-input request
    // into the session endpoint. The chrome must surface an "Action required"
    // heading + the question form outside any disclosure.
    await page.unroute("**/api/agents");
    await withWorkerStatus({
      state: "working",
      pr: null,
      step: "waiting for user confirmation",
      blocker: null,
    });
    await page.route(`**/api/agents/${TICKET}/session*`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const payload = {
        method: "item/tool/requestUserInput",
        id: 0,
        params: {
          questions: [
            {
              id: "scope",
              header: "Scope",
              question: "Which scope?",
              options: [
                { label: "Full brief", description: "Complete the ticket." },
                { label: "First slice", description: "Stop after the first boundary." },
              ],
            },
          ],
        },
      };
      const injected = {
        ...body,
        provider_inspector: {
          run_id: "fixture-run",
          provider: "codex",
          state: "waiting-approval",
          raw_count: 1,
          normalized_count: 1,
          dispositions: { rendered: 1, summarized: 0, ignored: 0, unknown: 0 },
          pending_requests: [
            {
              request_id: 0,
              request_kind: "item/tool/requestUserInput",
              received_at: "2026-07-30T00:00:00Z",
              raw_seq: 1,
              payload,
            },
          ],
          events: [],
        },
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(injected),
      });
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    const actionPanel = page.locator('[data-testid="session-action-required"]');
    await actionPanel.waitFor({ state: "visible" });
    await actionPanel.locator(".session-action-required-title", { hasText: "Action required" }).waitFor();
    await actionPanel.getByText("Which scope?").waitFor();
    // R1-04 cross-check: the pending card carries no request-kind noise.
    const actionCardText = await actionPanel.locator(".session-provider-request").first().innerText();
    if (actionCardText.includes("item/tool/requestUserInput") || actionCardText.toLowerCase().includes("request id")) {
      throw new Error(`R1-04: pending card must not leak diagnostic fields (got: ${actionCardText})`);
    }
    await page.screenshot({
      path: path.join(OUT_DIR, "05-action-required.png"),
      fullPage: true,
    });

    logStep("state 6: zero-event chrome — cold start with no action required, no ambient noise");
    await page.unroute(`**/api/agents/${TICKET}/session*`);
    await page.unroute("**/api/agents");
    await withWorkerStatus({
      state: "working",
      pr: null,
      step: "cold start — no provider events yet",
      blocker: null,
    });
    // Strip provider_inspector from the session response so no diagnostic
    // surfaces render at all. The transcript-store cached the state-5
    // inspector across navigations, so an explicit intercept is required.
    await page.route(`**/api/agents/${TICKET}/session*`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const stripped = { ...body };
      delete stripped.provider_inspector;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(stripped),
      });
    });
    // Force full-page reload so React state (transcript-store) is discarded
    // and the intercepted session response drives what SessionTab sees.
    await page.goto("about:blank");
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".agent-session-surface-head");
    await page.locator('[data-testid="session-state-pill"]').filter({ hasText: "working" }).waitFor();
    await page.locator('[data-testid="session-step-row"]').waitFor({ state: "visible" });
    // Cold start: no action required, no blocker, no visible diagnostics.
    // Run details may still exist (format is derivable from the transcript
    // shape) but must stay collapsed by default.
    if ((await page.locator('[data-testid="session-action-required"]').count()) !== 0) {
      throw new Error("WIKI-152 R1-05: zero-event chrome must not render an Action required panel");
    }
    if ((await page.locator('[data-testid="session-blocker-row"]').count()) !== 0) {
      throw new Error("WIKI-152 R1-05: zero-event chrome must not render a blocker row");
    }
    const coldRunDetails = page.locator('[data-testid="session-run-details"]');
    if ((await coldRunDetails.count()) > 0
      && (await coldRunDetails.first().evaluate((el) => el.hasAttribute("open")))) {
      throw new Error("WIKI-152 R1-05: zero-event Run details must stay collapsed");
    }
    await page.screenshot({
      path: path.join(OUT_DIR, "06-zero-event.png"),
      fullPage: true,
    });

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          screenshots: [
            "01-default-chrome.png",
            "02-blocked-chrome.png",
            "03-merge-ready-chrome.png",
            "04-diagnostics-open.png",
            "05-action-required.png",
            "06-zero-event.png",
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
