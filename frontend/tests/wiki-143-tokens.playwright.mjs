import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-143-tokens-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-143-tokens-");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;

try {
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(":root");

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
    readable: "760px",
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

  const scrollWidth = await page.evaluate(() => {
    const inner = document.createElement("div");
    inner.className = "session-scroll-inner";
    document.body.append(inner);
    const style = getComputedStyle(inner);
    const result = style.maxWidth;
    inner.remove();
    return result;
  });
  assert(scrollWidth === "760px", `session-scroll-inner max-width expected 760px, got ${scrollWidth}`);

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-143-tokens.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
