// WIKI-279: settings collapse to a single font, weight, and size that apply
// to every surface of the app. This test asserts:
//   - defaults: no weight persisted, all surfaces render at 400
//   - picking a face with multiple weights reveals detected-stop presets +
//     a free numeric input
//   - a chosen weight applies to EVERY surface (chrome, prose, mono) and to
//     both --font-single-weight and every legacy family weight var
//   - persistence: single `wiki-font` / `wiki-font-weight` / `wiki-font-size`
//     keys, arbitrary values (1–1000), reload survives, family change
//     preserves the saved weight
//   - legacy per-role keys (WIKI-244) migrate into the single keys once and
//     are then removed
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-105-font-weight-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-105-font-weight-");
const uiStatePath = path.join(fixtures.root, "ui-state.json");
const screenshots = {
  settings: path.join(OUT_DIR, "settings-weight-picker.png"),
  regular: path.join(OUT_DIR, "font-regular.png"),
  heavier: path.join(OUT_DIR, "font-heavier.png"),
};

let browser;
let backend;

try {
  console.error("[wiki-105] starting isolated single-font test");
  process.env.WIKI_UI_STATE_PATH = uiStatePath;
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  const defaults = await page.evaluate(() => {
    const modal = document.querySelector(".settings-modal");
    const preview = document.createElement("div");
    preview.className = "markdown-preview-view";
    preview.innerHTML = "<strong><code>inline code</code></strong>";
    document.body.append(preview);
    const codeWeight = getComputedStyle(preview.querySelector("code")).fontWeight;
    preview.remove();
    return {
      codeWeight,
      single: document.documentElement.style.getPropertyValue("--font-single-weight"),
      interface: document.documentElement.style.getPropertyValue("--font-interface-weight"),
      text: document.documentElement.style.getPropertyValue("--font-text-weight"),
      mono: document.documentElement.style.getPropertyValue("--font-monospace-weight"),
      width: modal?.clientWidth,
      scrollWidth: modal?.scrollWidth,
    };
  });
  if (defaults.codeWeight !== "600") {
    throw new Error(`Default bold inline code changed to ${defaults.codeWeight}`);
  }
  if (defaults.single || defaults.interface || defaults.text || defaults.mono) {
    throw new Error(`Default font weight should be unset: ${JSON.stringify(defaults)}`);
  }
  if (defaults.width !== defaults.scrollWidth) {
    throw new Error(`Settings modal overflowed: ${defaults.width} clientWidth vs ${defaults.scrollWidth} scrollWidth`);
  }

  // WIKI-279: the single Font row houses the picker + weight input. Picking a
  // face with multiple bundled weights reveals the detected-stop presets.
  const fontRow = page.locator(".settings-row").filter({ hasText: "Font" }).first();
  const fontPicker = fontRow.locator(".font-picker");
  await fontPicker.getByRole("button").click();
  await fontPicker.getByRole("option", { name: /Consolas for Powerline/ }).click();

  const weightInput = fontRow.locator(".font-weight-input");
  await page.waitForFunction(() => {
    const row = [...document.querySelectorAll(".settings-row")]
      .find((candidate) => candidate.querySelector(".font-picker"));
    return row ? row.querySelectorAll(".font-weight-stop").length > 1 : false;
  });
  const weights = await fontRow.locator(".font-weight-stop").evaluateAll(
    (stops) => stops.map((stop) => stop.textContent?.trim() ?? "")
  );
  if (!weights.includes("400") || !weights.includes("700")) {
    throw new Error(`Expected bundled Consolas faces to expose Regular and Bold, saw ${weights.join(", ")}`);
  }
  const selectedLayout = await page.locator(".settings-modal").evaluate((modal) => ({
    width: modal.clientWidth,
    scrollWidth: modal.scrollWidth,
  }));
  if (selectedLayout.width !== selectedLayout.scrollWidth) {
    throw new Error(
      `Weight selector overflowed settings: ${selectedLayout.width} clientWidth vs ${selectedLayout.scrollWidth} scrollWidth`
    );
  }

  await fontPicker.getByRole("button").click();
  await page.screenshot({ path: screenshots.settings, fullPage: true });
  await page.keyboard.press("Escape");
  await fontRow.locator(".font-weight-stop", { hasText: "400" }).click();
  await page.screenshot({ path: screenshots.regular, fullPage: true });
  await fontRow.locator(".font-weight-stop", { hasText: "700" }).click();
  await page.screenshot({ path: screenshots.heavier, fullPage: true });

  // WIKI-279: one weight applies EVERYWHERE — chrome (body), prose (markdown),
  // and mono (pane log) all pull the same value.
  const applied = await page.evaluate(() => {
    const text = document.createElement("div");
    text.className = "markdown-preview-view";
    const mono = document.createElement("pre");
    mono.className = "session-pane-log";
    document.body.append(text, mono);
    const result = {
      interface: getComputedStyle(document.body).fontWeight,
      text: getComputedStyle(text).fontWeight,
      mono: getComputedStyle(mono).fontWeight,
    };
    text.remove();
    mono.remove();
    return result;
  });
  if (applied.interface !== "700" || applied.text !== "700" || applied.mono !== "700") {
    throw new Error(`One weight must apply to every surface: ${JSON.stringify(applied)}`);
  }

  await page.waitForTimeout(2200);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  if ((await page.getByLabel("Font weight (1–1000)").inputValue()) !== "700") {
    throw new Error("Font weight did not persist through reload");
  }
  const persisted = JSON.parse(readFileSync(uiStatePath, "utf8"));
  if (persisted["wiki-font-weight"] !== "700") {
    throw new Error(`ui-state did not mirror selected weight: ${JSON.stringify(persisted)}`);
  }
  // Legacy per-role keys must not appear in the persisted state.
  for (const legacy of [
    "wiki-ui-font-weight",
    "wiki-text-font-weight",
    "wiki-agent-font-weight",
    "wiki-mono-font-weight",
  ]) {
    if (legacy in persisted) {
      throw new Error(`Legacy key ${legacy} must not persist under the single-font model`);
    }
  }

  const mirrorContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const mirrorPage = await mirrorContext.newPage();
  await mirrorPage.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await mirrorPage.getByLabel("Settings").waitFor();
  await mirrorPage.getByLabel("Settings").click();
  if ((await mirrorPage.getByLabel("Font weight (1–1000)").inputValue()) !== "700") {
    throw new Error("Fresh window did not hydrate the mirrored font weight");
  }
  await mirrorContext.close();

  // A saved weight is preserved as-is across family changes — arbitrary values
  // are first-class (variable fonts); static faces render the nearest declared
  // face.
  const rowAfterReload = page.locator(".settings-row").filter({ hasText: "Font" }).first();
  await rowAfterReload.locator(".font-picker").getByRole("button").click();
  await rowAfterReload.getByRole("option", { name: /JetBrains Mono/ }).click();
  await page.waitForFunction(() => {
    const input = document.querySelector('input[aria-label^="Font weight"]');
    return input instanceof HTMLInputElement && input.value === "700";
  });
  const appliedAfterChange = await page.evaluate(() =>
    document.documentElement.style.getPropertyValue("--font-single-weight")
  );
  if (appliedAfterChange !== "700") {
    throw new Error(`Family change must preserve the saved weight, applied ${appliedAfterChange || "nothing"}`);
  }

  // Arbitrary values are the core contract. Fill 650 and assert application,
  // persistence, ui-state mirroring, and family-change retention; then
  // exercise blur edge cases (empty reverts; 0 clamps to 1; 1001 clamps to
  // 1000).
  const arbitraryRow = page.locator(".settings-row").filter({ hasText: "Font" }).first();
  const arbitraryInput = arbitraryRow.locator(".font-weight-input");
  await arbitraryInput.fill("650");
  await arbitraryInput.blur();
  await page.waitForFunction(() =>
    document.documentElement.style.getPropertyValue("--font-single-weight") === "650");
  if (await page.evaluate(() => localStorage.getItem("wiki-font-weight")) !== "650") {
    throw new Error("Arbitrary weight 650 must persist to localStorage");
  }
  await page.waitForTimeout(2200);
  const mirrored650 = JSON.parse(readFileSync(uiStatePath, "utf8"));
  if (mirrored650["wiki-font-weight"] !== "650") {
    throw new Error(`ui-state did not mirror the arbitrary weight: ${JSON.stringify(mirrored650)}`);
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  if ((await page.getByLabel("Font weight (1–1000)").inputValue()) !== "650") {
    throw new Error("Arbitrary weight did not survive reload");
  }
  const row650 = page.locator(".settings-row").filter({ hasText: "Font" }).first();
  await row650.locator(".font-picker").getByRole("button").click();
  await row650.getByRole("option", { name: /Consolas for Powerline/ }).click();
  await page.waitForFunction(() => {
    const input = document.querySelector('input[aria-label^="Font weight"]');
    return input instanceof HTMLInputElement && input.value === "650";
  });
  if (await page.evaluate(() => document.documentElement.style.getPropertyValue("--font-single-weight")) !== "650") {
    throw new Error("Arbitrary weight must survive a family change");
  }

  const weightEdge = row650.locator(".font-weight-input");
  const expectAfterBlur = async (typed, expected, label) => {
    await weightEdge.fill(typed);
    await weightEdge.blur();
    await page.waitForTimeout(150);
    const value = await weightEdge.inputValue();
    if (value !== expected) {
      throw new Error(`${label}: expected ${expected} after blur, got ${value}`);
    }
    const applied = await page.evaluate(() => document.documentElement.style.getPropertyValue("--font-single-weight"));
    if (applied !== expected) {
      throw new Error(`${label}: expected applied weight ${expected}, got ${applied || "nothing"}`);
    }
  };
  await expectAfterBlur("", "650", "empty input");
  // Non-numeric text never reaches the component: input[type=number] rejects
  // it at the platform level (Playwright fill() refuses it for the same
  // reason), which leaves the empty-value path — covered above.
  await expectAfterBlur("1001", "1000", "above-range input clamps");
  await expectAfterBlur("0", "1", "zero clamps to the minimum");
  await weightEdge.fill("650");
  await weightEdge.blur();

  // Detected-stop presets hide when the face exposes a single weight; the
  // numeric input always renders.
  await page.evaluate(() => {
    localStorage.setItem("wiki-font", "Andale Mono");
    localStorage.removeItem("wiki-font-weight");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  const andaleRow = page.locator(".settings-row").filter({ hasText: "Font" }).first();
  await andaleRow.locator(".font-weight-input").waitFor({ state: "visible" });
  await page.waitForTimeout(800);
  if (await andaleRow.locator(".font-weight-stop").count()) {
    throw new Error("Single detected weight should hide the stop presets");
  }

  // Legacy migration: seeding via a WIKI-244-era per-role key must promote
  // into the single keys on first load and clean up the legacy keys. Purge
  // both localStorage AND the ui-state mirror so the pre-migration world has
  // only the legacy keys — otherwise the mirror re-hydrates the single keys
  // and migration correctly skips (which would be a real bug, not this test).
  await page.evaluate(() => localStorage.clear());
  writeFileSync(
    uiStatePath,
    JSON.stringify({
      "wiki-mono-font": "Menlo",
      "wiki-mono-font-weight": "500",
      "wiki-font-size-body": "17",
    }),
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  const migrated = await page.evaluate(() => ({
    font: localStorage.getItem("wiki-font"),
    weight: localStorage.getItem("wiki-font-weight"),
    size: localStorage.getItem("wiki-font-size"),
    legacyFont: localStorage.getItem("wiki-mono-font"),
    legacyWeight: localStorage.getItem("wiki-mono-font-weight"),
    legacySize: localStorage.getItem("wiki-font-size-body"),
  }));
  if (migrated.font !== "Menlo" || migrated.weight !== "500" || migrated.size !== "17") {
    throw new Error(`Legacy migration failed: ${JSON.stringify(migrated)}`);
  }
  if (migrated.legacyFont || migrated.legacyWeight || migrated.legacySize) {
    throw new Error(`Legacy keys must be cleared after migration: ${JSON.stringify(migrated)}`);
  }
  if (errors.length) throw new Error(errors.join("\n"));

  console.log(
    JSON.stringify(
      {
        fixturePaths: { root: fixtures.root, uiState: uiStatePath },
        screenshots,
        weights,
        applied,
      },
      null,
      2
    )
  );
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
