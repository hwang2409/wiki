import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-143";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-143-tokens-evidence";
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-143-tokens-");

try {
  const transcript = path.join(fixtures.root, "wiki-143-tokens.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-143-tokens" },
      codexAssistant("Chat message body for token cap measurement.", "2026-07-22T15:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  await page.addInitScript(() => {
    // WIKI-234: the opencode default theme intentionally re-skins radii to
    // 0. Pin the base theme here — this test guards the canonical token
    // scale, not per-theme overrides.
    localStorage.setItem("wiki-theme", "mono-light");
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
            layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-143" },
          },
        ],
      }),
    );
  });
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(":root");
  await page.locator(".agent-session-surface").waitFor({ state: "visible" });
  await page.locator(".session-scroll-inner").waitFor({ state: "visible" });

  const tokens = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      fs2xs: style.getPropertyValue("--fs-2xs").trim(),
      fsXs: style.getPropertyValue("--fs-xs").trim(),
      fsSm: style.getPropertyValue("--fs-sm").trim(),
      fsMd: style.getPropertyValue("--fs-md").trim(),
      fsBase: style.getPropertyValue("--fs-base").trim(),
      fsLg: style.getPropertyValue("--fs-lg").trim(),
      fsXl: style.getPropertyValue("--fs-xl").trim(),
      fs2xl: style.getPropertyValue("--fs-2xl").trim(),
      fs3xl: style.getPropertyValue("--fs-3xl").trim(),
      fs4xl: style.getPropertyValue("--fs-4xl").trim(),
      fs5xl: style.getPropertyValue("--fs-5xl").trim(),
      fwRegular: style.getPropertyValue("--fw-regular").trim(),
      fwMedium: style.getPropertyValue("--fw-medium").trim(),
      fwSemibold: style.getPropertyValue("--fw-semibold").trim(),
      fwDisplay: style.getPropertyValue("--fw-display").trim(),
      radiusXs: style.getPropertyValue("--radius-xs").trim(),
      radiusSm: style.getPropertyValue("--radius-sm").trim(),
      radiusMd: style.getPropertyValue("--radius-md").trim(),
      radiusLg: style.getPropertyValue("--radius-lg").trim(),
      radiusXl: style.getPropertyValue("--radius-xl").trim(),
      radiusFull: style.getPropertyValue("--radius-full").trim(),
      readable: style.getPropertyValue("--readable-col").trim(),
    };
  });

  const expected = {
    fs2xs: "10px",
    fsXs: "11px",
    fsSm: "12px",
    fsMd: "13px",
    fsBase: "14px",
    fsLg: "15px",
    fsXl: "16px",
    fs2xl: "18px",
    fs3xl: "22px",
    fs4xl: "28px",
    fs5xl: "34px",
    fwRegular: "400",
    fwMedium: "500",
    fwSemibold: "600",
    fwDisplay: "700",
    radiusXs: "4px",
    radiusSm: "6px",
    radiusMd: "8px",
    radiusLg: "10px",
    radiusXl: "12px",
    radiusFull: "999px",
    readable: "1024px",
  };
  for (const [key, value] of Object.entries(expected)) {
    assert(
      tokens[key] === value,
      `token --${key} expected ${value}, got ${JSON.stringify(tokens[key])}`,
    );
  }

  const scaleBinding = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.fontSize = "var(--fs-md)";
    probe.style.fontWeight = "var(--fw-medium)";
    probe.style.borderRadius = "var(--radius-md)";
    document.body.append(probe);
    const style = getComputedStyle(probe);
    const result = {
      fontSize: style.fontSize,
      fontWeight: style.fontWeight,
      borderRadius: style.borderRadius,
    };
    probe.remove();
    return result;
  });
  assert(scaleBinding.fontSize === "13px", `--fs-md expected 13px, got ${scaleBinding.fontSize}`);
  assert(scaleBinding.fontWeight === "500", `--fw-medium expected 500, got ${scaleBinding.fontWeight}`);
  assert(
    scaleBinding.borderRadius === "8px",
    `--radius-md expected 8px, got ${scaleBinding.borderRadius}`,
  );

  const measureChatMaxWidth = () =>
    page.evaluate(() => {
      const inner = document.querySelector(".agent-session-surface.is-full .session-scroll-inner");
      if (!(inner instanceof HTMLElement)) return null;
      const style = getComputedStyle(inner);
      return {
        maxWidth: style.maxWidth,
        actualWidth: Math.round(inner.getBoundingClientRect().width),
      };
    });

  const baseline = await measureChatMaxWidth();
  assert(baseline, "could not find .agent-session-surface.is-full .session-scroll-inner in live DOM");
  assert(
    baseline.maxWidth === "1024px",
    `real chat .session-scroll-inner max-width expected 1024px, got ${baseline.maxWidth}`,
  );
  assert(
    baseline.actualWidth <= 1024,
    `chat inner width expected <=1024, got ${baseline.actualWidth}`,
  );

  await page.evaluate(() => {
    document.documentElement.style.setProperty("--readable-col", "900px");
  });
  const mutated = await measureChatMaxWidth();
  assert(
    mutated?.maxWidth === "900px",
    `mutation flip: expected 900px after overriding --readable-col, got ${mutated?.maxWidth}`,
  );
  await page.evaluate(() => {
    document.documentElement.style.removeProperty("--readable-col");
  });
  const restored = await measureChatMaxWidth();
  assert(
    restored?.maxWidth === "1024px",
    `restore: expected 1024px after removing override, got ${restored?.maxWidth}`,
  );

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-143-tokens.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
