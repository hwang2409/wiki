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

const TICKET = "WIKI-145";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-145-typography-evidence";
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-145-typography-");

try {
  const transcript = path.join(fixtures.root, "wiki-145-typography.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-145-typography" },
      codexAssistant("Typography smoke fixture.", "2026-07-22T15:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "false");
  });
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(":root");

  // Sample 1: probe --fs-3xl bound to page header token.
  const fs3xl = await page.evaluate(() => {
    const probe = document.createElement("h1");
    probe.className = "inline-title";
    probe.textContent = "Probe";
    document.body.appendChild(probe);
    const styleSelf = getComputedStyle(probe);
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--fs-3xl").trim();
    const out = { computed: styleSelf.fontSize, weight: styleSelf.fontWeight, raw };
    probe.remove();
    return out;
  });
  assert(
    fs3xl.computed === fs3xl.raw,
    `.inline-title font-size expected ${fs3xl.raw} (var(--fs-3xl)), got ${fs3xl.computed}`,
  );
  assert(
    fs3xl.weight === "600",
    `.inline-title font-weight expected 600 (semibold), got ${fs3xl.weight}`,
  );

  // Sample 2: dashboard section heading uses the control tier and semibold.
  const dashboardHead = await page.evaluate(() => {
    const wrap = document.createElement("div");
    wrap.className = "dashboard-header";
    const h2 = document.createElement("h2");
    h2.textContent = "Probe";
    wrap.appendChild(h2);
    document.body.appendChild(wrap);
    const cs = getComputedStyle(h2);
    const controlProbe = document.createElement("div");
    controlProbe.style.fontSize = "var(--font-control-size)";
    document.body.appendChild(controlProbe);
    const raw = getComputedStyle(controlProbe).fontSize;
    const out = { computed: cs.fontSize, weight: cs.fontWeight, raw };
    controlProbe.remove();
    wrap.remove();
    return out;
  });
  assert(
    dashboardHead.computed === dashboardHead.raw,
    `dashboard header h2 expected ${dashboardHead.raw}, got ${dashboardHead.computed}`,
  );
  assert(
    dashboardHead.weight === "600",
    `dashboard header h2 weight expected 600, got ${dashboardHead.weight}`,
  );

  // Sample 3: WIKI-151 renamed .nav-agents-divider -> .nav-agents-group-title
  // (labeled Active/History groups). Still --fs-xs uppercase.
  const divider = await page.evaluate(() => {
    const el = document.createElement("div");
    el.className = "nav-agents-group-title";
    const label = document.createElement("span");
    label.textContent = "archived";
    el.appendChild(label);
    document.body.appendChild(el);
    const cs = getComputedStyle(el);
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--fs-xs").trim();
    const out = {
      computed: cs.fontSize,
      raw,
      textTransform: cs.textTransform,
      letterSpacing: cs.letterSpacing,
    };
    el.remove();
    return out;
  });
  assert(
    divider.computed === divider.raw,
    `nav-agents-group-title expected ${divider.raw} (--fs-xs), got ${divider.computed}`,
  );
  assert(
    divider.textTransform === "uppercase",
    `nav-agents-group-title expected uppercase, got ${divider.textTransform}`,
  );

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-145-typography.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
