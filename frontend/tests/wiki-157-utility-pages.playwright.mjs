import { mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const REPO_SHOTS = new URL("../../docs/screenshots/wiki-157/", import.meta.url).pathname;
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || REPO_SHOTS;
const TICKET = "WIKI-157";

function payloadActivity() {
  return [
    {
      sha: "abc0123456789def0123456789abcdef01234567",
      date: "2026-07-30T09:15:00+00:00",
      message: "seed reference notes",
      files: [
        { path: "vault/notes/hot.md", status: "M" },
        { path: "vault/notes/plan.md", status: "A" },
      ],
    },
    {
      sha: "fed9876543210cba9876543210fedcba98765432",
      date: "2026-07-30T08:02:00+00:00",
      message: "restructure meta index",
      files: [{ path: "vault/meta/map.md", status: "M" }],
    },
  ];
}

function payloadLinks() {
  return {
    "notes/hot.md": { outgoing: ["notes/plan.md"], incoming: [], unresolved: ["future.md"] },
    "notes/plan.md": { outgoing: [], incoming: ["notes/hot.md"], unresolved: [] },
    "meta/map.md": { outgoing: ["notes/hot.md"], incoming: [], unresolved: [] },
  };
}

function payloadTokens() {
  const hours = 8;
  const buckets = [];
  const now = Date.parse("2026-07-30T09:00:00Z");
  for (let i = 0; i < hours; i += 1) {
    const ts = new Date(now - (hours - i - 1) * 3600 * 1000).toISOString();
    // Deliberately omit "reasoning" + "cached" from most buckets to exercise
    // the "unavailable" convention in the totals column.
    buckets.push({
      ts,
      series: {
        "claude/opus-4-7": { input: 1400 + i * 60, output: 900 + i * 40 },
        "codex/gpt-5.4": { input: 700 + i * 30, output: 450 + i * 20 },
      },
    });
  }
  return {
    buckets,
    totals: { input: 0, cached: 0, output: 0, reasoning: 0 },
    models: ["opus-4-7", "gpt-5.4"],
    clis: ["claude", "codex"],
    sessions_scanned: 24,
    bucket: "hour",
    refreshing: false,
  };
}

async function stubApi(page) {
  await page.route("**/api/activity**", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(payloadActivity()) }),
  );
  await page.route("**/api/links**", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(payloadLinks()) }),
  );
  await page.route("**/api/tokens**", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(payloadTokens()) }),
  );
}

async function stubApiError(page) {
  await page.route("**/api/tokens**", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "backend unreachable" }) }),
  );
}

async function assertUtilityChrome(page, label) {
  await page.getByRole("heading", { name: label, exact: true }).waitFor({ state: "visible" });
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-157-utility-");
  writeRegistry(fixtures.registryPath, [[TICKET, path.join(fixtures.root, "transcript.jsonl")]]);
  writeQueue(fixtures.queuePath, TICKET);
  process.env.WIKI_UI_STATE_PATH = path.join(fixtures.root, "ui-state.json");
  process.env.WIKI_TOKEN_CACHE_PATH = path.join(fixtures.root, "token-cache.json");

  let browser;
  let backend;
  try {
    backend = await startBackend(fixtures);
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1500, height: 940 } });
    const page = await context.newPage();
    await stubApi(page);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    for (const [hash, label, name, extra] of [
      ["#/activity", "Activity feed", "activity"],
      ["#/graph", "Graph view", "graph"],
      ["#/health", "Vault health", "health"],
      ["#/tokens", "Token usage", "tokens"],
    ]) {
      await page.goto(`${backend.baseUrl}/${hash}`, { waitUntil: "domcontentloaded" });
      await assertUtilityChrome(page, label);
      // Give charts a beat to lay out.
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(OUT_DIR, `${name}.png`), fullPage: false });
    }

    // Regression: tokens page shows the "unavailable" label instead of a false zero.
    await page.goto(`${backend.baseUrl}/#/tokens`, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "Token usage" }).waitFor();
    const unavailableCount = await page.getByText("unavailable", { exact: true }).count();
    if (unavailableCount < 2) {
      throw new Error(
        `Expected tokens page to render "unavailable" for reasoning + cached; got ${unavailableCount}`,
      );
    }

    // Regression: no primary "git" / "commit" / "wiki lint" copy in the four
    // utility-page bodies (secondary sha rendering is fine, that's tested via
    // vitest).
    for (const hash of ["#/activity", "#/graph", "#/health", "#/tokens"]) {
      await page.goto(`${backend.baseUrl}/${hash}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(200);
      const forbidden = ["wiki lint", "git commit", " commit ", "run the CLI"];
      const bodyText = await page.locator(".utility-page").innerText();
      for (const phrase of forbidden) {
        if (bodyText.toLowerCase().includes(phrase.toLowerCase())) {
          throw new Error(`${hash}: forbidden primary phrase "${phrase.trim()}" present`);
        }
      }
    }

    // Graph list-mode screenshot.
    await page.goto(`${backend.baseUrl}/#/graph`, { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: /^List$/i }).click();
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(OUT_DIR, "graph-list.png"), fullPage: false });

    // Tokens error-state screenshot: retry surface visible.
    const errorContext = await browser.newContext({ viewport: { width: 1500, height: 940 } });
    const errorPage = await errorContext.newPage();
    await stubApiError(errorPage);
    await errorPage.goto(`${backend.baseUrl}/#/tokens`, { waitUntil: "domcontentloaded" });
    await errorPage.getByRole("alert").waitFor({ state: "visible" });
    await errorPage.getByRole("button", { name: /Retry/i }).waitFor({ state: "visible" });
    await errorPage.screenshot({ path: path.join(OUT_DIR, "tokens-error.png"), fullPage: false });
    await errorContext.close();

    if (errors.length > 0) throw new Error(errors.join("\n"));
    console.log("WIKI-157 utility page screenshots: PASS");
    console.log(`WIKI-157 screenshots in ${OUT_DIR}`);
  } finally {
    if (browser) await browser.close();
    if (backend) await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
