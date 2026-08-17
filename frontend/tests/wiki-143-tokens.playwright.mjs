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
      fsXl: style.getPropertyValue("--fs-xl").trim(),
      fs2xl: style.getPropertyValue("--fs-2xl").trim(),
      fs3xl: style.getPropertyValue("--fs-3xl").trim(),
      fs4xl: style.getPropertyValue("--fs-4xl").trim(),
      fs5xl: style.getPropertyValue("--fs-5xl").trim(),
      fwRegular: style.getPropertyValue("--fw-regular").trim(),
      fwMedium: style.getPropertyValue("--fw-medium").trim(),
      fwSemibold: style.getPropertyValue("--fw-semibold").trim(),
      fwDisplay: style.getPropertyValue("--fw-display").trim(),
      proseSize: style.getPropertyValue("--font-prose-size").trim(),
      controlSize: style.getPropertyValue("--font-control-size").trim(),
      chromeSize: style.getPropertyValue("--font-chrome-size").trim(),
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
    fsXl: "16px",
    fs2xl: "18px",
    fs3xl: "22px",
    fs4xl: "28px",
    fs5xl: "34px",
    fwRegular: "400",
    fwMedium: "500",
    fwSemibold: "600",
    fwDisplay: "600",
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

  const legacyTierSizes = await page.evaluate(() => {
    const host = document.createElement("div");
    host.style.display = "contents";
    document.body.append(host);
    const names = ["2xs", "xs", "sm", "md", "base", "lg"];
    const values = Object.fromEntries(
      names.map((name) => {
        const node = document.createElement("span");
        node.style.fontSize = `var(--fs-${name})`;
        host.append(node);
        return [name, getComputedStyle(node).fontSize];
      }),
    );
    host.remove();
    return values;
  });
  assert(
    JSON.stringify(legacyTierSizes) ===
      JSON.stringify({ "2xs": "10px", xs: "10px", sm: "13px", md: "13px", base: "15px", lg: "15px" }),
    `legacy size aliases drifted: ${JSON.stringify(legacyTierSizes)}`,
  );

  const semanticSizes = await page.evaluate(() => {
    const classes = ["session-assistant", "session-tool", "metadata-property", "session-pane-log"];
    const nodes = classes.map((className) => {
      const node = document.createElement("div");
      node.className = className;
      document.body.append(node);
      return node;
    });
    const values = nodes.map((node) => getComputedStyle(node).fontSize);
    const families = [getComputedStyle(document.body).fontFamily, getComputedStyle(nodes[3]).fontFamily];
    nodes.forEach((node) => node.remove());
    return { values, families };
  });
  assert(
    JSON.stringify(semanticSizes.values) === JSON.stringify(["15px", "13px", "10px", "13px"]),
    `semantic type tiers changed: ${JSON.stringify(semanticSizes.values)}`,
  );
  assert(
    semanticSizes.families[0].startsWith('"Inter Variable"'),
    `new-install chrome family changed: ${JSON.stringify(semanticSizes.families)}`,
  );
  assert(
    semanticSizes.families[1].startsWith('"Fira Code"'),
    `new-install mono family changed: ${JSON.stringify(semanticSizes.families)}`,
  );

  const scaledSizes = await page.evaluate(() => {
    document.documentElement.style.setProperty("--font-single-size", "18px");
    const classes = ["session-assistant", "session-tool", "metadata-property"];
    const nodes = classes.map((className) => {
      const node = document.createElement("div");
      node.className = className;
      document.body.append(node);
      return node;
    });
    const values = nodes.map((node) => getComputedStyle(node).fontSize);
    nodes.forEach((node) => node.remove());
    document.documentElement.style.removeProperty("--font-single-size");
    return values;
  });
  assert(
    JSON.stringify(scaledSizes) === JSON.stringify(["18px", "15.6px", "12px"]),
    `semantic tiers did not scale together: ${JSON.stringify(scaledSizes)}`,
  );

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

  const surfaceSizes = await page.evaluate(() => {
    const host = document.createElement("div");
    host.style.display = "contents";
    document.body.append(host);
    const results = {};
    const add = (surface, className, tag = "span", parent = host) => {
      const node = document.createElement(tag);
      node.className = className;
      node.textContent = "probe";
      parent.append(node);
      (results[surface] ??= []).push({ className, fontSize: getComputedStyle(node).fontSize });
      return node;
    };

    add("agents", "status-badge");
    add("agents", "nav-agents-group-title");
    const sessionStrip = document.createElement("div");
    sessionStrip.className = "session-state-strip";
    host.append(sessionStrip);
    add("session", "session-state-meta", "span", sessionStrip);
    const sessionHead = document.createElement("div");
    sessionHead.className = "agent-session-surface-head";
    host.append(sessionHead);
    add("session", "session-state-pill", "span", sessionHead);

    const dialog = document.createElement("div");
    dialog.className = "bb-dialog";
    host.append(dialog);
    add("dialogs", "bb-dialog__title", "h2", dialog);
    add("dialogs", "bb-dialog__description", "p", dialog);
    add("dialogs", "bb-dialog__rail-item", "button", dialog);
    add("dialogs", "bb-dialog__group-title", "h3", dialog);
    add("dialogs", "bb-dialog__group-hint", "p", dialog);
    add("dialogs", "bb-button bb-button--md", "button", dialog);

    add("settings", "settings-row-name", "div");
    add("settings", "settings-row-desc", "div");
    add("settings", "settings-reset", "button");
    add("settings", "theme-choice-name");
    add("spawn", "agent-spawn-label");
    add("spawn", "agent-spawn-select", "select");
    add("spawn", "agent-spawn-hint");
    add("spawn", "agent-spawn-preview-primary");
    add("spawn", "agent-spawn-preview-line");

    const dashboardTable = add("dashboard", "dashboard-table", "table");
    add("dashboard", "dashboard-table-cell", "td", dashboardTable);
    add("dashboard", "dashboard-stale-label");
    add("dashboard", "dashboard-filter-option", "label");
    add("dashboard", "cost-row", "div");
    add("dashboard", "cost-panel-section-title", "div");
    add("tokens", "tokens-chip", "button");
    add("tokens", "tokens-filter-label");
    add("tokens", "tokens-legend");
    add("tokens", "tokens-total-value");
    const kanbanContent = add("kanban", "markdown-preview-view kanban-card-content", "div");
    add("kanban", "priority-badge", "span", kanbanContent);
    add("kanban", "kanban-column-title");
    add("kanban", "kanban-column-count");
    add("kanban", "kanban-empty");
    add("toasts", "bb-toast__title");
    add("toasts", "bb-toast__description");
    add("status-strip", "status-bar");
    add("status-strip", "tmux-status-item");
    host.remove();
    return results;
  });
  const expectedSurfaceSizes = {
    agents: ["10px", "10px"],
    session: ["10px", "10px"],
    dialogs: ["15px", "13px", "13px", "10px", "13px", "13px"],
    settings: ["13px", "10px", "10px", "10px"],
    spawn: ["10px", "13px", "10px", "13px", "10px"],
    dashboard: ["13px", "13px", "10px", "10px", "13px", "10px"],
    tokens: ["13px", "10px", "10px", "15px"],
    kanban: ["13px", "13px", "13px", "10px", "13px"],
    toasts: ["13px", "10px"],
    "status-strip": ["10px", "10px"],
  };
  for (const [surface, expectedSizes] of Object.entries(expectedSurfaceSizes)) {
    const actualSizes = surfaceSizes[surface].map(({ fontSize }) => fontSize);
    assert(
      JSON.stringify(actualSizes) === JSON.stringify(expectedSizes),
      `${surface} semantic sizes changed: ${JSON.stringify(actualSizes)}`,
    );
  }

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
