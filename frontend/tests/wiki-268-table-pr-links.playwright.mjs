import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const PR_URL_A = "https://github.com/phoebehq/phoebe/pull/13557";
const PR_URL_B = "https://github.com/phoebehq/phoebe/pull/13606";
const PR_URL_C = "https://github.com/henrywang/wiki/pull/200";
const COMMIT_URL = "https://github.com/henrywang/wiki/commit/dd3898b";

async function main() {
  const fixtures = makeFixtureRoot("wiki-268-table-pr-links-");
  const vault = path.join(fixtures.root, "vault");
  const notePath = path.join(vault, "fleet.md");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(
    notePath,
    [
      "# Fleet status",
      "",
      "See [full pr](" + PR_URL_A + ") for the merge word thread.",
      "",
      "| ticket | pr | note |",
      "| --- | --- | --- |",
      "| PHO-15330 | [pr](" + PR_URL_B + ") | monitor start |",
      "| WIKI-264 | [pr](" + PR_URL_C + ") | fonts landed |",
      "| WIKI-266 | [commit](" + COMMIT_URL + ") | codex tools |",
      "",
    ].join("\n"),
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-268", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
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
              layout: { kind: "pane", id: "pane-1", path: "fleet.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/fleet.md`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".markdown-preview-view table");

    // Prose PR link must render as the full card (or its loading fallback,
    // which still lives inside the card path — checked separately in vitest).
    const proseCard = await page.locator(`.markdown-preview-view p a.gh-preview-card[href="${PR_URL_A}"]`).count();
    const proseFallback = await page.locator(`.markdown-preview-view p a.external-link[href="${PR_URL_A}"]`).count();
    assert(proseCard + proseFallback > 0, "prose PR link should render via the card path (card or its loading fallback)");

    // Table-cell PR links must render as the compact inline form.
    for (const url of [PR_URL_B, PR_URL_C]) {
      const inline = page.locator(`.markdown-preview-view td a.gh-preview-inline[href="${url}"]`);
      await inline.waitFor({ state: "visible" });
      assert(
        (await page.locator(`.markdown-preview-view td a.gh-preview-card[href="${url}"]`).count()) === 0,
        `table-cell PR link should NOT render as a card: ${url}`,
      );
      const box = await inline.boundingBox();
      assert(box && box.height < 32, `inline PR link should stay on one row (got height ${box?.height})`);
    }

    // Commit gets the same treatment with is-commit variant.
    const commitInline = page.locator(`.markdown-preview-view td a.gh-preview-inline.is-commit[href="${COMMIT_URL}"]`);
    await commitInline.waitFor({ state: "visible" });
    assert(
      (await commitInline.textContent())?.includes("wiki@dd3898b"),
      "commit inline label should be repo@shortSha",
    );

    // Row height sanity: with 3 PRs/commit, each table row must remain
    // single-line — regression guard for the card-in-cell layout blowup.
    const rowHeights = await page.locator(".markdown-preview-view tbody tr").evaluateAll(
      (rows) => rows.map((row) => row.getBoundingClientRect().height),
    );
    for (const height of rowHeights) {
      assert(height < 44, `table row must stay single-line, got height ${height}`);
    }
    if (process.env.WIKI268_SCREENSHOT) {
      await page.locator(".markdown-preview-view").screenshot({ path: process.env.WIKI268_SCREENSHOT });
    }
  } finally {
    await browser.close();
    await backend.stop();
  }
  console.log("WIKI-268 playwright: table cells render compact GitHub refs; prose keeps the card path");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
