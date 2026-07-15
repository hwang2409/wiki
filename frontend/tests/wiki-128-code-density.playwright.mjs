import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const LONG_LINE = "x".repeat(240);
const SCREENSHOT_DIR = process.env.WIKI_128_SCREENSHOT_DIR;
const FILES = {
  "long.py": `value = "${LONG_LINE}"\n`,
  "long.txt": `${LONG_LINE}\n`,
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function metrics(page, selector) {
  return page.locator(selector).evaluate((element) => {
    const style = getComputedStyle(element);
    const line = element.querySelector(".code-file-highlighted .line, .code-file-line");
    const lineStyle = line ? getComputedStyle(line) : null;
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      overflowX: style.overflowX,
      lineHeight: lineStyle?.lineHeight,
      lineMinHeight: lineStyle?.minHeight,
    };
  });
}

async function captureScreenshots(page) {
  if (!SCREENSHOT_DIR) return;
  await fs.mkdir(SCREENSHOT_DIR, { recursive: true });
  const cases = [
    ["long.py", ".code-file-highlighted .line", "shiki"],
    ["long.txt", ".code-file-line", "plain"],
  ];
  const oldStyles = `
    .code-file-scroll { overflow-x: hidden; }
    .code-file-source, .code-file-highlighted { padding: 8px 0 24px; line-height: 1.55; }
    .code-file-line, .code-file-highlighted .line { min-height: 1.55em; line-height: normal; }
    .code-file-highlighted .line::before, .code-file-gutter { width: 58px; margin-right: 12px; padding-right: 8px; }
  `;
  for (const [file, selector, kind] of cases) {
    for (const theme of ["light", "dark"]) {
      await page.evaluate((nextTheme) => localStorage.setItem("wiki-theme", nextTheme), `mono-${theme}`);
      await page.goto(`${page.url().split("#")[0]}#/file/${file}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector(selector);
      await page.evaluate((nextTheme) => {
        document.documentElement.dataset.theme = nextTheme;
      }, `mono-${theme}`);
      await page.screenshot({ path: path.join(SCREENSHOT_DIR, `wiki-128-after-${kind}-${theme}.png`) });
      const style = await page.addStyleTag({ content: oldStyles });
      await page.locator(".code-file-scroll").evaluate((element) => element.classList.add("view-content"));
      await page.screenshot({ path: path.join(SCREENSHOT_DIR, `wiki-128-before-${kind}-${theme}.png`) });
      await style.evaluate((element) => element.remove());
    }
  }
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-128-code-density-");
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-128", []);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });

  try {
    await page.route("**/api/files/tree", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          files: Object.entries(FILES).map(([path, content]) => ({
            path,
            size: content.length,
            updated_at: "2026-07-15T00:00:00Z",
          })),
          truncated: false,
        }),
      })
    );
    await page.route("**/api/files/content**", (route) => {
      const path = new URL(route.request().url()).searchParams.get("path");
      const content = path ? FILES[path] : undefined;
      return route.fulfill({
        contentType: "application/json",
        status: content === undefined ? 404 : 200,
        body: JSON.stringify(
          content === undefined
            ? { detail: "File not found" }
            : { path, size: content.length, content, binary: false, error: null }
        ),
      });
    });

    await page.goto(`${backend.baseUrl}/#/file/long.py`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".code-file-highlighted .line");
    const shiki = await metrics(page, ".code-file-scroll");
    assert(shiki.overflowX === "auto", `Shiki overflow-x is ${shiki.overflowX}`);
    assert(shiki.scrollWidth > shiki.clientWidth, `Shiki line is not horizontally scrollable: ${JSON.stringify(shiki)}`);

    await page.goto(`${backend.baseUrl}/#/file/long.txt`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".code-file-line");
    const fallback = await metrics(page, ".code-file-scroll");
    assert(fallback.overflowX === "auto", `Fallback overflow-x is ${fallback.overflowX}`);
    assert(
      fallback.scrollWidth > fallback.clientWidth,
      `Fallback line is not horizontally scrollable: ${JSON.stringify(fallback)}`
    );
    assert(
      shiki.lineHeight === fallback.lineHeight && shiki.lineMinHeight === fallback.lineMinHeight,
      `Shiki/fallback row heights diverged: shiki=${JSON.stringify(shiki)} fallback=${JSON.stringify(fallback)}`
    );

    await captureScreenshots(page);

    console.log(`shiki metrics: ${JSON.stringify(shiki)}`);
    console.log(`fallback metrics: ${JSON.stringify(fallback)}`);
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
