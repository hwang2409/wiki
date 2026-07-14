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

  const monoPicker = page.locator(".font-picker").nth(2);
  await monoPicker.getByRole("button").click();
  await monoPicker.getByRole("option", { name: /Consolas for Powerline/ }).click();

  const monoWeight = page.getByLabel("Monospace font weight");
  await page.waitForFunction(
    () => document.querySelectorAll('select[aria-label="Monospace font weight"] option').length > 1
  );
  const weights = await monoWeight.locator("option").evaluateAll((options) => options.map((option) => option.value));
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
  await monoWeight.selectOption("400");
  await page.screenshot({ path: screenshots.regular, fullPage: true });
  await monoWeight.selectOption("700");
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

  const pickerAfterReload = page.locator(".font-picker").nth(2);
  await pickerAfterReload.getByRole("button").click();
  await pickerAfterReload.getByRole("option", { name: /JetBrains Mono/ }).click();
  await page.waitForFunction(() => {
    const select = document.querySelector('select[aria-label="Monospace font weight"]');
    return select instanceof HTMLSelectElement && [...select.options].some((option) => option.value === select.value);
  });
  const selectedAfterChange = await page.getByLabel("Monospace font weight").inputValue();
  const availableAfterChange = await page
    .getByLabel("Monospace font weight")
    .locator("option")
    .evaluateAll((options) => options.map((option) => option.value));
  if (!availableAfterChange.includes(selectedAfterChange)) {
    throw new Error("Family change left an invalid selected weight");
  }
  const appliedAfterChange = await page.evaluate(() =>
    document.documentElement.style.getPropertyValue("--font-monospace-weight")
  );
  if (appliedAfterChange !== selectedAfterChange) {
    throw new Error(`Family reset displayed ${selectedAfterChange} but applied ${appliedAfterChange || "nothing"}`);
  }

  await page.evaluate(() => {
    localStorage.setItem("wiki-mono-font", "Andale Mono");
    localStorage.removeItem("wiki-mono-font-weight");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByLabel("Settings").waitFor();
  await page.getByLabel("Settings").click();
  if (await page.getByLabel("Monospace font weight").count()) {
    throw new Error("Single detected weight should hide the selector");
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
