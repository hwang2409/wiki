// One-shot WIKI-305 visual verification. Boots an isolated backend with a
// user + assistant transcript and screenshots the .session-user card, the
// assistant prose, and (where present) an .session-action-required panel at
// four themes: mono-light, mono-dark, opencode, tokyo-night. Also asserts
// key style properties are in the WIKI-305 contract.

import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-305";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.join(path.dirname(new URL(import.meta.url).pathname), "evidence", "wiki-305");
mkdirSync(OUT_DIR, { recursive: true });

const THEMES = ["mono-light", "mono-dark", "opencode", "tokyo-night"];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-305-verify-");

try {
  const transcript = path.join(fixtures.root, "wiki-305.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-305" },
      codexUser("Please refactor @thread:pending-interactions banner.", "2026-08-17T15:00:00Z"),
      codexAssistant(
        "The banner now uses the bb detail-card shape. Approvals surface a primary CTA and dismiss stays quiet.",
        "2026-08-17T15:00:05Z",
      ),
      codexUser("Great — how do you handle multi-question forms?", "2026-08-17T15:00:20Z"),
      codexAssistant(
        "Each question renders as its own labelled control. Send is a bb Button default; errors surface below the actions row.",
        "2026-08-17T15:00:25Z",
      ),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  try {
    backend = await startBackend(fixtures, { healthTimeoutMs: 60000 });
  } catch (e) {
    console.error("backend startup failed:", e);
    throw e;
  }
  browser = await chromium.launch({ headless: true });

  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.route(`**/api/agents/${TICKET}/session*`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
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
                request_id: 1,
                request_kind: "item/tool/requestUserInput",
                received_at: "2026-08-17T15:00:30Z",
                raw_seq: 1,
                payload: {
                  method: "item/tool/requestUserInput",
                  params: {
                    questions: [
                      {
                        id: "scope",
                        header: "Scope",
                        question: "Which scope should the refactor use?",
                        options: [
                          { label: "Small patch", description: "Keep the change narrow." },
                          { label: "Full pass", description: "Update the full thread body." },
                        ],
                      },
                    ],
                  },
                },
              },
            ],
            events: [],
          },
        }),
      });
    });
    await page.addInitScript((themeName) => {
      localStorage.setItem("wiki-theme", themeName);
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
              layout: { kind: "pane", id: "pane-1", path: `agent://${"WIKI-305"}` },
            },
          ],
        }),
      );
    }, theme);
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor();
    await page.locator(".session-user", { hasText: "Please refactor" }).first().waitFor();
    assert(
      await page.locator('.session-user .prompt-mention-pill[data-mention="@thread:pending-interactions"]').count() === 1,
      `[${theme}] user turn should render mention text as a prompt pill`,
    );
    const actionPanel = page.locator('[data-testid="session-action-required"]');
    await actionPanel.waitFor();
    assert((await actionPanel.evaluate((el) => el.classList.contains("bb-detail-card"))), `[${theme}] action card must use bb detail-card grammar`);
    assert((await actionPanel.locator(".bb-button--default").count()) === 1, `[${theme}] action card needs one primary CTA`);
    assert((await actionPanel.locator(".bb-button--ghost").count()) === 1, `[${theme}] action card needs one ghost dismiss control`);

    const userCard = await page
      .locator(".session-user", { hasText: "Please refactor" })
      .first()
      .evaluate((el) => {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {
          borderTopWidth: style.borderTopWidth,
          borderLeftWidth: style.borderLeftWidth,
          borderRightWidth: style.borderRightWidth,
          borderBottomWidth: style.borderBottomWidth,
          borderRadius: style.borderRadius,
          alignSelf: style.alignSelf,
          background: style.backgroundColor,
          right: Math.round(window.innerWidth - rect.right),
        };
      });

    const assistantLine = await page
      .locator(".session-assistant p", { hasText: "The banner now uses" })
      .first()
      .evaluate((el) => {
        const style = getComputedStyle(el);
        return {
          lineHeight: style.lineHeight,
          fontSize: style.fontSize,
        };
      });

    console.log(`[wiki-305] ${theme} raw:`, JSON.stringify(userCard));

    assert(userCard.borderTopWidth === "1px" && userCard.borderLeftWidth === "1px", `[${theme}] hairline border expected on user card, got L:${userCard.borderLeftWidth} T:${userCard.borderTopWidth}`);
    assert(Number.parseFloat(userCard.borderRadius) >= 6, `[${theme}] user card radius >=6, got ${userCard.borderRadius}`);
    assert(userCard.alignSelf === "flex-end", `[${theme}] user card must align right, got ${userCard.alignSelf}`);
    assert(userCard.right >= 0 && userCard.right < 260, `[${theme}] user card should hug right edge, got offset ${userCard.right}px`);
    assert(assistantLine.fontSize === "15px", `[${theme}] assistant font should be 15px, got ${assistantLine.fontSize}`);
    assert(assistantLine.lineHeight === "22px", `[${theme}] assistant line-height should be 1.375rem, got ${assistantLine.lineHeight}`);
    console.log(`[wiki-305] theme=${theme} user card { border=hairline radius=${userCard.borderRadius} alignSelf=${userCard.alignSelf} rightOffset=${userCard.right}px background=${userCard.background} } assistant line-height=${assistantLine.lineHeight} @ ${assistantLine.fontSize}`);

    await page.screenshot({ path: path.join(OUT_DIR, `session-${theme}.png`), fullPage: false });
    await page.close();
  }

  console.log("[wiki-305] all themes verified");
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
