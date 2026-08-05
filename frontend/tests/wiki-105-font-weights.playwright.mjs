import { mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-105-font-weight-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-105-font-weight-");
const uiStatePath = path.join(fixtures.root, "ui-state.json");
const screenshots = {
  settings: path.join(OUT_DIR, "settings-weight-picker.png"),
  regular: path.join(OUT_DIR, "mono-regular.png"),
  heavier: path.join(OUT_DIR, "mono-heavier.png"),
};

let browser;
let backend;

try {
  console.error("[wiki-105] starting isolated font-weight test");
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
  if (defaults.interface || defaults.text || defaults.mono) {
    throw new Error(`Default font weights should be unset: ${JSON.stringify(defaults)}`);
  }
  if (defaults.width !== defaults.scrollWidth) {
    throw new Error(`Settings modal overflowed: ${defaults.width} clientWidth vs ${defaults.scrollWidth} scrollWidth`);
  }

  // WIKI-244: the agent chat font role sits between text and mono, and the
  // weight control is a free numeric input plus detected-stop presets.
  const monoRow = page.locator(".settings-row").filter({ hasText: "Monospace font" }).first();
  const monoPicker = monoRow.locator(".font-picker");
  await monoPicker.getByRole("button").click();
  await monoPicker.getByRole("option", { name: /Consolas for Powerline/ }).click();

  const monoWeight = monoRow.locator(".font-weight-input");
  await page.waitForFunction(() => {
    const row = [...document.querySelectorAll(".settings-row")]
      .find((candidate) => candidate.textContent?.includes("Monospace font"));
    return row ? row.querySelectorAll(".font-weight-stop").length > 1 : false;
  });
  const weights = await monoRow.locator(".font-weight-stop").evaluateAll(
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

  await monoPicker.getByRole("button").click();
  await page.screenshot({ path: screenshots.settings, fullPage: true });
  await page.keyboard.press("Escape");
  await monoRow.locator(".font-weight-stop", { hasText: "400" }).click();
  await page.screenshot({ path: screenshots.regular, fullPage: true });
  await monoRow.locator(".font-weight-stop", { hasText: "700" }).click();
  await page.screenshot({ path: screenshots.heavier, fullPage: true });

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
  if (applied.mono !== "700" || applied.text !== "400" || applied.interface !== "400") {
    throw new Error(`Unexpected surface weights: ${JSON.stringify(applied)}`);
  }

  await page.waitForTimeout(2200);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  if ((await page.getByLabel("Monospace font weight").inputValue()) !== "700") {
    throw new Error("Monospace weight did not persist through reload");
  }
  const persisted = JSON.parse(readFileSync(uiStatePath, "utf8"));
  if (persisted["wiki-mono-font-weight"] !== "700") {
    throw new Error(`ui-state did not mirror selected weight: ${JSON.stringify(persisted)}`);
  }

  const mirrorContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const mirrorPage = await mirrorContext.newPage();
  await mirrorPage.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await mirrorPage.getByLabel("Settings").waitFor();
  await mirrorPage.getByLabel("Settings").click();
  if ((await mirrorPage.getByLabel("Monospace font weight").inputValue()) !== "700") {
    throw new Error("Fresh window did not hydrate the mirrored monospace weight");
  }
  await mirrorContext.close();

  // WIKI-244: a saved weight is preserved as-is across family changes —
  // arbitrary values are first-class (variable fonts); static faces render
  // the nearest declared face.
  const rowAfterReload = page.locator(".settings-row").filter({ hasText: "Monospace font" }).first();
  await rowAfterReload.locator(".font-picker").getByRole("button").click();
  await rowAfterReload.getByRole("option", { name: /JetBrains Mono/ }).click();
  await page.waitForFunction(() => {
    const input = document.querySelector('input[aria-label^="Monospace font weight"]');
    return input instanceof HTMLInputElement && input.value === "700";
  });
  const appliedAfterChange = await page.evaluate(() =>
    document.documentElement.style.getPropertyValue("--font-monospace-weight")
  );
  if (appliedAfterChange !== "700") {
    throw new Error(`Family change must preserve the saved weight, applied ${appliedAfterChange || "nothing"}`);
  }

  // WIKI-244 review M2: arbitrary values are the core contract. Fill 650 and
  // assert application, persistence, ui-state mirroring, and family-change
  // retention; then exercise blur edge cases.
  const arbitraryRow = page.locator(".settings-row").filter({ hasText: "Monospace font" }).first();
  const arbitraryInput = arbitraryRow.locator(".font-weight-input");
  await arbitraryInput.fill("650");
  await arbitraryInput.blur();
  await page.waitForFunction(() =>
    document.documentElement.style.getPropertyValue("--font-monospace-weight") === "650");
  if (await page.evaluate(() => localStorage.getItem("wiki-mono-font-weight")) !== "650") {
    throw new Error("Arbitrary weight 650 must persist to localStorage");
  }
  await page.waitForTimeout(2200);
  const mirrored650 = JSON.parse(readFileSync(uiStatePath, "utf8"));
  if (mirrored650["wiki-mono-font-weight"] !== "650") {
    throw new Error(`ui-state did not mirror the arbitrary weight: ${JSON.stringify(mirrored650)}`);
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  if ((await page.getByLabel("Monospace font weight").inputValue()) !== "650") {
    throw new Error("Arbitrary weight did not survive reload");
  }
  const row650 = page.locator(".settings-row").filter({ hasText: "Monospace font" }).first();
  await row650.locator(".font-picker").getByRole("button").click();
  await row650.getByRole("option", { name: /Consolas for Powerline/ }).click();
  await page.waitForFunction(() => {
    const input = document.querySelector('input[aria-label^="Monospace font weight"]');
    return input instanceof HTMLInputElement && input.value === "650";
  });
  if (await page.evaluate(() => document.documentElement.style.getPropertyValue("--font-monospace-weight")) !== "650") {
    throw new Error("Arbitrary weight must survive a family change");
  }

  // Blur edge cases: empty and non-numeric revert to the last weight; 0 and
  // 1001 clamp into the valid 1-1000 range.
  const weightEdge = row650.locator(".font-weight-input");
  const expectAfterBlur = async (typed, expected, label) => {
    await weightEdge.fill(typed);
    await weightEdge.blur();
    await page.waitForTimeout(150);
    const value = await weightEdge.inputValue();
    if (value !== expected) {
      throw new Error(`${label}: expected ${expected} after blur, got ${value}`);
    }
    const applied = await page.evaluate(() => document.documentElement.style.getPropertyValue("--font-monospace-weight"));
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

  await page.evaluate(() => {
    localStorage.setItem("wiki-mono-font", "Andale Mono");
    localStorage.removeItem("wiki-mono-font-weight");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  // WIKI-244: the numeric weight input always renders; only the detected-stop
  // presets hide when the face exposes a single weight.
  const andaleRow = page.locator(".settings-row").filter({ hasText: "Monospace font" }).first();
  await andaleRow.locator(".font-weight-input").waitFor({ state: "visible" });
  await page.waitForTimeout(800);
  if (await andaleRow.locator(".font-weight-stop").count()) {
    throw new Error("Single detected weight should hide the stop presets");
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
