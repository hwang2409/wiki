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

async function openNote(browser, backend, targetPath) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.addInitScript((notePath) => {
    localStorage.setItem(
      "wiki-window-layout-v2",
      JSON.stringify({
        version: 2,
        activeWindowId: "window-0",
        windows: [
          {
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: notePath },
          },
        ],
      }),
    );
  }, targetPath);
  await page.goto(`${backend.baseUrl}/#/note/${targetPath}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".markdown-preview-view");
  return page;
}

async function measureFadeOverTime(page, selector, samples) {
  return page.evaluate(
    async ({ selector, samples }) => {
      const opacities = [];
      for (let index = 0; index < samples; index += 1) {
        await new Promise((resolve) => setTimeout(resolve, 60));
        const node = document.querySelector(selector);
        opacities.push(node ? Number(getComputedStyle(node).opacity) : null);
      }
      return opacities;
    },
    { selector, samples },
  );
}

async function scenarioPreloadedMeta(browser, backend) {
  const page = await openNote(browser, backend, "note.md");
  try {
    // Delay the sharp image so we get a visible loading window during which
    // the preview must sit at opacity 1 above the frame.
    await page.route(/\/api\/vault\/assets\/hero\.png($|\?)/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 350));
      return route.continue();
    });
    const frame = page.locator(".markdown-image-frame").first();
    await frame.waitFor({ state: "visible", timeout: 5000 });

    const before = await frame.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return {
        height: rect.height,
        aspectRatio: getComputedStyle(node).aspectRatio,
      };
    });
    assert(before.height >= 20, `preloaded frame reserved no height (got ${before.height})`);
    assert(
      before.aspectRatio.replace(/\s+/g, "") === "320/200" || before.aspectRatio === "1.6",
      `preloaded frame aspect ratio must lock before load, got '${before.aspectRatio}'`,
    );

    const image = frame.locator("img[decoding='async']");
    await image.evaluate((img) => new Promise((resolve) => {
      if (img.complete && img.naturalWidth > 0) return resolve();
      img.addEventListener("load", () => resolve(), { once: true });
      img.addEventListener("error", () => resolve(), { once: true });
    }));

    const after = await frame.evaluate((node) => ({ height: node.getBoundingClientRect().height }));
    const heightDrift = Math.abs(after.height - before.height);
    assert(
      heightDrift < 1,
      `preloaded frame height shifted after image load: ${before.height} -> ${after.height}`,
    );
    console.error("[wiki-189-cls] preloaded scenario: frame height locked");
  } finally {
    await page.close();
  }
}

async function scenarioMetadataAfterImage(browser, backend) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  try {
    // Strip asset_meta from the note payload so the frontend has to fetch
    // metadata on its own. Route MUST be installed before the note fetch.
    await page.route(/\/api\/notes\/note\.md/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.asset_meta = {};
      return route.fulfill({
        status: response.status(),
        headers: response.headers(),
        body: JSON.stringify(body),
      });
    });
    // Delay the asset-meta probe long enough that the sharp image lands
    // FIRST — this is the round-3 race condition the review demanded a
    // fixture for.
    let metaFetchedAt = null;
    await page.route(/\/api\/vault\/asset-meta\//, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 900));
      metaFetchedAt = Date.now();
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
    const image = frame.locator("img[decoding='async']");
    await image.evaluate((img) => new Promise((resolve) => {
      if (img.complete && img.naturalWidth > 0) return resolve();
      img.addEventListener("load", () => resolve(), { once: true });
      img.addEventListener("error", () => resolve(), { once: true });
    }));
    // Ratio must be locked from naturalWidth/Height NOW, before meta arrives.
    const atLoad = await frame.evaluate((node) => ({
      height: node.getBoundingClientRect().height,
      aspectRatio: getComputedStyle(node).aspectRatio,
    }));
    assert(atLoad.height >= 20, `frame reserved no height on image load (got ${atLoad.height})`);
    // Give the delayed asset-meta probe time to arrive.
    await page.waitForTimeout(1200);
    const afterMeta = await frame.evaluate((node) => ({
      height: node.getBoundingClientRect().height,
    }));
    const heightDrift = Math.abs(afterMeta.height - atLoad.height);
    assert(
      heightDrift < 1,
      `race scenario: metadata arrival shifted frame height ${atLoad.height} -> ${afterMeta.height}`,
    );
    if (metaFetchedAt === null) {
      console.error("[wiki-189-cls] race scenario: (asset-meta was served from note payload)");
    }
    console.error("[wiki-189-cls] race scenario: metadata-after-image did NOT shift layout");
  } finally {
    await page.close();
  }
}

async function scenarioPreviewFadeOpacity(browser, backend) {
  // Fresh context so browser cache/previous routes don't taint the timing.
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  try {
    // A large image with a long delay guarantees an observable "loading"
    // window during which the preview must sit at opacity 1 above the
    // sharp image.
    await page.route(/\/api\/vault\/assets\/big\.png($|\?)/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
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
              layout: { kind: "pane", id: "pane-1", path: "note-big.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/note-big.md`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".markdown-preview-view");

    const preview = page.locator(".markdown-image-preview").first();
    await preview.waitFor({ state: "attached", timeout: 5000 });
    // Sample opacity immediately: while the sharp image is still loading
    // (~1.5s available), the preview must be at opacity ~1.
    const opacityLoading = await preview.evaluate((node) => Number(getComputedStyle(node).opacity));
    assert(opacityLoading > 0.9, `preview should start at ~1.0 opacity, got ${opacityLoading}`);
    // Let the sharp image finish loading + the fade transition run, then
    // sample every 40ms and confirm we crossed a mid-fade opacity value.
    const opacities = await page.evaluate(async () => {
      const samples = [];
      // Wait for the preview to be told to fade (is-fading class flips on
      // when the image loads).
      for (let i = 0; i < 60; i += 1) {
        const node = document.querySelector(".markdown-image-preview");
        if (node && node.className.includes("is-fading")) break;
        await new Promise((resolve) => setTimeout(resolve, 40));
      }
      for (let i = 0; i < 18; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 40));
        const node = document.querySelector(".markdown-image-preview");
        samples.push(node ? Number(getComputedStyle(node).opacity) : null);
      }
      return samples;
    });
    const nonNull = opacities.filter((value) => value !== null);
    assert(nonNull.length > 0, `preview vanished before fade could be measured: ${JSON.stringify(opacities)}`);
    const sawIntermediate = nonNull.some((value) => value > 0.05 && value < 0.95);
    assert(
      sawIntermediate,
      `preview never showed a mid-fade opacity: ${JSON.stringify(nonNull)}`,
    );
    console.error(`[wiki-189-cls] fade scenario: opacity trajectory ${JSON.stringify(nonNull)}`);
  } finally {
    await page.close();
    await context.close();
  }
}

async function scenarioEncodedDelimiters(browser, backend) {
  // End-to-end proof that filenames containing percent-encoded `#` and `?`
  // ride through the backend note-metadata pass AND the frontend resolver
  // without either side truncating them. The buggy resolver would strip
  // the tail after decode, produce no vault candidate, and leave the
  // frame stuck in shimmer.
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
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
              layout: { kind: "pane", id: "pane-1", path: "note-encoded.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/note-encoded.md`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".markdown-preview-view");
    const frames = page.locator(".markdown-image-frame");
    await frames.first().waitFor({ state: "visible", timeout: 5000 });
    const count = await frames.count();
    assert(count === 2, `expected 2 image frames, got ${count}`);

    for (const [index, expected] of [
      [0, "/api/vault/assets/hero%23draft.png"],
      [1, "/api/vault/assets/hero%3Fdraft.png"],
    ]) {
      const frame = frames.nth(index);
      const img = frame.locator("img[decoding='async']");
      const src = await img.getAttribute("src");
      assert(
        src === expected,
        `frame ${index} resolved to ${src}, expected ${expected}`,
      );
      // The dimensions from the note.asset_meta payload must be on the img
      // (proving the resolver + backend both round-tripped the literal `#`
      // / `?` in the filename).
      const width = await img.getAttribute("width");
      const height = await img.getAttribute("height");
      assert(width && Number(width) > 0, `frame ${index} lost width`);
      assert(height && Number(height) > 0, `frame ${index} lost height`);
      const hasKnownRatio = await frame.evaluate((node) => node.className.includes("has-known-ratio"));
      assert(hasKnownRatio, `frame ${index} did not receive has-known-ratio (metadata got dropped)`);
      // Wait for the sharp image to finish loading — proves the encoded
      // URL is actually served by the backend, not just a valid string.
      await img.evaluate((node) => new Promise((resolve, reject) => {
        if (node.complete && node.naturalWidth > 0) return resolve();
        node.addEventListener("load", () => resolve(), { once: true });
        node.addEventListener("error", () => reject(new Error("image failed to load")), { once: true });
      }));
    }
    console.error("[wiki-189-cls] encoded-delimiter scenario: %23 and %3F filenames served + rendered");
  } finally {
    await page.close();
  }
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-189-cls-");
  const vault = path.join(fixtures.root, "vault");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(path.join(vault, "hero.png"), greyPng(320, 200));
  // Big enough that the backend's preview generator emits a base64 preview
  // (>24px long side) so the fade path actually renders.
  await fs.writeFile(path.join(vault, "big.png"), greyPng(640, 400));
  // Literal `#` and `?` in filenames — the same filenames the backend +
  // frontend delimiter fixture tests round-trip.
  await fs.writeFile(path.join(vault, "hero#draft.png"), greyPng(320, 200));
  await fs.writeFile(path.join(vault, "hero?draft.png"), greyPng(320, 200));
  await fs.writeFile(
    path.join(vault, "note.md"),
    ["# CLS fixture", "", "![hero](hero.png)", ""].join("\n"),
    "utf-8",
  );
  await fs.writeFile(
    path.join(vault, "note-big.md"),
    ["# Fade fixture", "", "![big](big.png)", ""].join("\n"),
    "utf-8",
  );
  await fs.writeFile(
    path.join(vault, "note-encoded.md"),
    [
      "# Encoded delimiter fixture",
      "",
      "![hash](hero%23draft.png)",
      "",
      "![question](hero%3Fdraft.png)",
      "",
    ].join("\n"),
    "utf-8",
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-189", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  try {
    await scenarioPreloadedMeta(browser, backend);
    await scenarioMetadataAfterImage(browser, backend);
    await scenarioPreviewFadeOpacity(browser, backend);
    await scenarioEncodedDelimiters(browser, backend);
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
