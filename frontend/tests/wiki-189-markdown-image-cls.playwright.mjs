import fs from "node:fs/promises";
import path from "node:path";
import { crc32, deflateSync } from "node:zlib";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

// A minimally-valid solid-grey PNG at the requested dimensions.
function greyPng(width, height) {
  const scanlines = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 3;
      scanlines[offset] = 200;
      scanlines[offset + 1] = 200;
      scanlines[offset + 2] = 200;
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

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-189-cls-");
  const vault = path.join(fixtures.root, "vault");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(path.join(vault, "hero.png"), greyPng(320, 200));
  await fs.writeFile(
    path.join(vault, "note.md"),
    ["# CLS fixture", "", "![hero](hero.png)", ""].join("\n"),
    "utf-8",
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-189", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  try {
    // Throttle the raw asset response so we can observe the loading state.
    await page.route(/\/api\/vault\/assets\/hero\.png($|\?)/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 350));
      return route.continue();
    });
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
              layout: { kind: "pane", id: "pane-1", path: "note.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/note.md`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".markdown-preview-view");

    const frame = page.locator(".markdown-image-frame").first();
    await frame.waitFor({ state: "visible", timeout: 5000 });

    const before = await frame.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return {
        top: rect.top,
        height: rect.height,
        aspectRatio: getComputedStyle(node).aspectRatio,
      };
    });
    assert(before.height >= 20, `frame reserved no height while loading (got ${before.height})`);
    assert(
      before.aspectRatio.replace(/\s+/g, "") === "320/200" || before.aspectRatio === "1.6",
      `frame aspect ratio must lock before load, got '${before.aspectRatio}'`,
    );

    const image = frame.locator("img[decoding='async']");
    await image.evaluate((img) => new Promise((resolve) => {
      if (img.complete && img.naturalWidth > 0) return resolve();
      img.addEventListener("load", () => resolve(), { once: true });
      img.addEventListener("error", () => resolve(), { once: true });
    }));

    const after = await frame.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { top: rect.top, height: rect.height };
    });
    // The frame's own layout must not jump — height stays locked from the
    // aspect ratio committed on first render. (Unrelated ancestor async
    // content may shift the whole preview up/down; that's not our race.)
    const heightDrift = Math.abs(after.height - before.height);
    assert(
      heightDrift < 1,
      `frame height shifted after image load: ${before.height} -> ${after.height}`,
    );
    console.error("[wiki-189-cls] frame height locked before and after image load");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
