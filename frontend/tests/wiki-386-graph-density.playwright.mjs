import fs from "node:fs/promises";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const NODE_COUNT = 124;
const SPARSE_NODE_COUNT = 8;
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.resolve("../docs/pr-screenshots/wiki-386");
const TICKET = "WIKI-386";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function seedVault(vaultDir, count) {
  await fs.rm(vaultDir, { recursive: true, force: true });
  await fs.mkdir(vaultDir, { recursive: true });
  for (let index = 1; index <= count; index += 1) {
    const current = `note-${String(index).padStart(2, "0")}.md`;
    const next = `note-${String((index % count) + 1).padStart(2, "0")}`;
    await fs.writeFile(
      path.join(vaultDir, current),
      `# ${current}\n\n[[${next}]]\n`,
    );
  }
}

async function installCanvasProbe(context) {
  await context.addInitScript(() => {
    window.__wikiGraphLabels = [];
    window.__wikiGraphFrame = [];
    const isGraphCanvas = (context) => context.canvas?.classList.contains("graph-canvas");
    const originalClearRect = CanvasRenderingContext2D.prototype.clearRect;
    const originalFillText = CanvasRenderingContext2D.prototype.fillText;
    const originalArc = CanvasRenderingContext2D.prototype.arc;
    CanvasRenderingContext2D.prototype.clearRect = function (...args) {
      if (isGraphCanvas(this)) window.__wikiGraphFrame = [];
      return originalClearRect.apply(this, args);
    };
    CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
      if (isGraphCanvas(this)) window.__wikiGraphLabels.push(String(text));
      return originalFillText.call(this, text, ...args);
    };
    CanvasRenderingContext2D.prototype.arc = function (x, y, radius, ...args) {
      if (isGraphCanvas(this)) window.__wikiGraphFrame.push({ x, y, radius });
      return originalArc.call(this, x, y, radius, ...args);
    };
  });
}

async function graphLabels(page) {
  return page.evaluate(() => [...new Set(window.__wikiGraphLabels ?? [])]);
}

async function graphFrame(page) {
  return page.evaluate(() => ({
    canvas: (() => {
      const element = document.querySelector(".graph-canvas");
      if (!(element instanceof HTMLCanvasElement)) return null;
      const rect = element.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    })(),
    arcs: window.__wikiGraphFrame ?? [],
  }));
}

async function waitForGraph(page) {
  await page.getByRole("heading", { name: "Graph view", exact: true }).waitFor();
  await page.getByText(/72 notes|73 notes|124 notes|8 notes/).waitFor();
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-386-graph-density-");
  const vaultDir = path.join(fixtures.root, "vault");
  await fs.writeFile(path.join(fixtures.root, "transcript.jsonl"), "");
  writeRegistry(fixtures.registryPath, [[TICKET, path.join(fixtures.root, "transcript.jsonl")]]);
  writeQueue(fixtures.queuePath, TICKET);
  await seedVault(vaultDir, NODE_COUNT);
  process.env.WIKI_VAULT_DIR = vaultDir;

  let browser;
  let backend;
  try {
    backend = await startBackend(fixtures);
    browser = await chromium.launch({ headless: true });
    let context = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    await installCanvasProbe(context);
    let page = await context.newPage();
    page.setDefaultTimeout(8_000);

    await page.goto(`${backend.baseUrl}/#/graph`, { waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    await page.evaluate(() => localStorage.removeItem("wiki-graph-mode"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForGraph(page);

    let listButton = page.getByRole("button", { name: /^List$/i });
    let canvasButton = page.getByRole("button", { name: /^Canvas$/i });
    assert(await listButton.getAttribute("aria-pressed") === "true", "dense graph should default to List");
    await page.locator(".graph-list").waitFor();

    await canvasButton.click();
    await page.locator(".graph-canvas").waitFor();
    await page.waitForTimeout(6_000);
    assert((await page.locator(".graph-canvas").getAttribute("data-graph-density")) === "dense", "large graph should mark canvas as dense");
    assert((await graphLabels(page)).length === 0, "dense canvas should not draw labels before focus");

    const canvasBox = await page.locator(".graph-canvas").boundingBox();
    const hoverNodeArcs = (await graphFrame(page)).arcs.slice(-NODE_COUNT);
    assert(canvasBox && hoverNodeArcs.length === NODE_COUNT, "dense graph should expose settled node positions");
    let hoveredNote40 = false;
    for (const arc of hoverNodeArcs) {
      await page.evaluate(() => {
        window.__wikiGraphLabels = [];
      });
      await page.mouse.move(canvasBox.x + arc.x, canvasBox.y + arc.y);
      await page.waitForTimeout(20);
      if ((await graphLabels(page)).includes("note-40")) {
        hoveredNote40 = true;
        break;
      }
    }
    assert(hoveredNote40, "hover should label note 40");

    await page.evaluate(() => {
      window.__wikiGraphLabels = [];
    });
    await page.locator(".graph-view").focus();
    await page.locator(".graph-view").press("ArrowRight");
    await page.waitForTimeout(150);
    const focusedLabels = await graphLabels(page);
    assert(focusedLabels.length > 0 && focusedLabels.length < NODE_COUNT, "dense canvas should label only the focus neighborhood");
    assert(focusedLabels.includes("note-01"), "keyboard focus should label note 1");
    assert(!focusedLabels.includes("note-40"), `dense canvas should keep distant labels hidden: ${focusedLabels.join(", ")}`);

    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    assert(await canvasButton.getAttribute("aria-pressed") === "true", "explicit Canvas choice should persist across reload");
    await listButton.click();
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    assert(await listButton.getAttribute("aria-pressed") === "true", "explicit List choice should persist across reload");

    await seedVault(vaultDir, 72);
    const boundary72Api = await fetch(`${backend.baseUrl}/api/links`).then((response) => response.json());
    assert(Object.keys(boundary72Api).length === 72, "exactly 72 notes should remain at the sparse boundary");
    await seedVault(vaultDir, 73);
    const boundary73Api = await fetch(`${backend.baseUrl}/api/links`).then((response) => response.json());
    assert(Object.keys(boundary73Api).length === 73, "exactly 73 notes should cross the dense boundary");

    await seedVault(vaultDir, SPARSE_NODE_COUNT);
    const sparseApi = await fetch(`${backend.baseUrl}/api/links`).then((response) => response.json());
    assert(Object.keys(sparseApi).length === SPARSE_NODE_COUNT, `fixture should contain ${SPARSE_NODE_COUNT} notes, got ${Object.keys(sparseApi).length}`);
    await context.close();
    context = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    await installCanvasProbe(context);
    page = await context.newPage();
    page.setDefaultTimeout(8_000);
    listButton = page.getByRole("button", { name: /^List$/i });
    canvasButton = page.getByRole("button", { name: /^Canvas$/i });
    await page.goto(`${backend.baseUrl}/#/graph`, { waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    assert(
      await canvasButton.getAttribute("aria-pressed") === "true",
      `sparse graph should default to Canvas (canvas=${await canvasButton.getAttribute("aria-pressed")}, list=${await listButton.getAttribute("aria-pressed")}, stored=${await page.evaluate(() => localStorage.getItem("wiki-graph-mode"))}, subtitle=${await page.locator(".utility-page-subtitle").innerText()})`,
    );
    await page.locator(".graph-canvas").waitFor();
    await page.waitForTimeout(250);
    assert((await page.locator(".graph-canvas").getAttribute("data-graph-density")) === "sparse", "small graph should mark canvas as sparse");
    const sparseLabels = await graphLabels(page);
    assert(sparseLabels.length === SPARSE_NODE_COUNT, "sparse canvas should draw every node label");

    await page.getByRole("button", { name: "Zoom in" }).click();
    await page.waitForTimeout(100);
    await page.getByRole("button", { name: "Fit graph" }).click();
    await page.waitForTimeout(1_200);
    const frame = await graphFrame(page);
    assert(frame.canvas && frame.arcs.length >= SPARSE_NODE_COUNT, "Fit graph should draw every sparse node");
    const nodeArcs = frame.arcs.slice(-SPARSE_NODE_COUNT);
    const minX = Math.min(...nodeArcs.map((arc) => arc.x - arc.radius));
    const maxX = Math.max(...nodeArcs.map((arc) => arc.x + arc.radius));
    const minY = Math.min(...nodeArcs.map((arc) => arc.y - arc.radius));
    const maxY = Math.max(...nodeArcs.map((arc) => arc.y + arc.radius));
    assert(minX >= 0 && maxX <= frame.canvas.width && minY >= 0 && maxY <= frame.canvas.height, "Fit graph should keep all nodes inside the canvas");

    await seedVault(vaultDir, NODE_COUNT);
    await context.close();
    context = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    await installCanvasProbe(context);
    page = await context.newPage();
    page.setDefaultTimeout(8_000);
    await page.goto(`${backend.baseUrl}/#/graph`, { waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    await page.evaluate(() => localStorage.setItem("wiki-graph-mode", "canvas"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForGraph(page);
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-386-after.png"), fullPage: false });
    console.log("WIKI-386 graph density: PASS");
    console.log(`WIKI-386 screenshot in ${OUT_DIR}`);
  } finally {
    if (browser) await browser.close();
    if (backend) await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
