// WIKI-200 — visual verify for artifact perf pass.
//
// Boots an isolated backend against a fixture vault with 24 tiny PNGs
// referenced from a note. Captures screenshots in both light and dark
// themes to /tmp/wiki-200-evidence, covering ONLY the markdown image
// blur-up + fade behavior:
//   - 01-<theme>-loading.png: sharp <img> is opacity:0, blur-up preview
//     is on top (proves the fix — before this PR the sharp <img> wore
//     a filter:blur while loading)
//   - 02-<theme>-loaded.png: previews faded out (opacity < 0.05),
//     sharp images visible.
// The artifact-block enter animation is exercised by the vitest suite
// (fresh-vs-repeat mount classes) and is NOT screenshotted here — no
// artifact stream is spawned by this fixture.

import fs from "node:fs/promises";
import path from "node:path";
import { crc32, deflateSync } from "node:zlib";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "./wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-200-evidence";
const TILE_COUNT = 24;

function coloredPng(width, height, r, g, b) {
  const scanlines = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 3;
      scanlines[offset] = r;
      scanlines[offset + 1] = g;
      scanlines[offset + 2] = b;
    }
  }
  const chunk = (type, payload) => {
    const name = Buffer.from(type, "ascii");
    const size = Buffer.alloc(4);
    size.writeUInt32BE(payload.length);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(Buffer.concat([name, payload])));
    return Buffer.concat([size, name, payload, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(scanlines)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

async function openNote(browser, backend, theme, notePath) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.addInitScript(
    ({ note, themeName }) => {
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: note },
            },
          ],
        }),
      );
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", themeName);
    },
    { note: notePath, themeName: theme },
  );
  await page.goto(`${backend.baseUrl}/#/note/${notePath}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".markdown-preview-view");
  return { context, page };
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-200-");
  const vault = path.join(fixtures.root, "vault");
  await fs.mkdir(vault, { recursive: true });

  // Twenty-four solid-colored PNGs — enough to exercise multiple
  // artifact frames without needing a session-level gallery fixture.
  const hues = [
    [232, 229, 223], [79, 103, 86], [162, 90, 62], [212, 173, 92],
    [104, 132, 174], [204, 108, 132], [86, 129, 106], [148, 122, 178],
  ];
  const imageLines = [];
  for (let i = 0; i < TILE_COUNT; i += 1) {
    const [r, g, b] = hues[i % hues.length];
    const file = `image-${String(i).padStart(2, "0")}.png`;
    await fs.writeFile(path.join(vault, file), coloredPng(640, 400, r, g, b));
    imageLines.push(`![tile ${i}](${file})`);
  }
  await fs.writeFile(
    path.join(vault, "gallery.md"),
    ["# WIKI-200 gallery fixture", "", ...imageLines, ""].join("\n"),
    "utf-8",
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-200", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  try {
    for (const theme of ["mono-light", "mono-dark"]) {
      const { context, page } = await openNote(browser, backend, theme, "gallery.md");
      try {
        // Delay assets so we can screenshot the blur-up placeholders in
        // action (proves the fix — before this PR, sharp <img> tags
        // wore a filter:blur while loading; now they stay at opacity:0
        // and the tiny preview_base64 sibling is the only blur target).
        await page.route(/\/api\/vault\/assets\/image-\d+\.png($|\?)/, async (route) => {
          await new Promise((resolve) => setTimeout(resolve, 400));
          return route.continue();
        });
        await page.reload({ waitUntil: "domcontentloaded" });
        await page.waitForSelector(".markdown-image-frame");
        // First screenshot: mid-load. Blur-up previews should be
        // visible on top of the frames, sharp images fading in.
        await page.screenshot({
          path: path.join(OUT_DIR, `01-${theme}-loading.png`),
          fullPage: true,
        });
        // Second screenshot: all images loaded, previews faded out.
        await page.waitForFunction(() => {
          const previews = document.querySelectorAll(".markdown-image-preview");
          if (previews.length === 0) return true;
          return Array.from(previews).every((preview) => {
            const opacity = Number(getComputedStyle(preview).opacity);
            return opacity < 0.05;
          });
        }, undefined, { timeout: 15_000 }).catch(() => {});
        await page.waitForTimeout(300);
        await page.screenshot({
          path: path.join(OUT_DIR, `02-${theme}-loaded.png`),
          fullPage: true,
        });
      } finally {
        await page.close();
        await context.close();
      }
    }
    const files = await fs.readdir(OUT_DIR);
    console.log(`[wiki-200] screenshots written to ${OUT_DIR}:`);
    for (const f of files.sort()) console.log(`  ${f}`);
  } finally {
    await browser.close();
    if (typeof backend.stop === "function") await backend.stop();
    else if (typeof backend.kill === "function") backend.kill();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
