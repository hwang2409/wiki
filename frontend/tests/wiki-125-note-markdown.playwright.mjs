import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-125-note-markdown-");
  const vault = path.join(fixtures.root, "vault");
  const notePath = path.join(vault, "nested", "rendering.md");
  await fs.mkdir(path.dirname(notePath), { recursive: true });
  await fs.mkdir(path.join(vault, "attachments"), { recursive: true });
  await fs.writeFile(path.join(vault, "nested", "image.png"), PNG_1X1);
  await fs.writeFile(path.join(vault, "attachments", "parent.png"), PNG_1X1);
  await fs.writeFile(path.join(vault, "image.png"), PNG_1X1);
  await fs.writeFile(path.join(vault, "fallback.png"), PNG_1X1);
  await fs.writeFile(
    notePath,
    [
      "# Rendering fixture",
      "",
      "![relative](image.png)",
      "![parent](../attachments/parent.png)",
      "![fallback](fallback.png)",
      "![[image.png|300]]",
      "![external](https://example.com/image.png)",
      "![scheme](//cdn.example.com/image.png)",
      "![absolute](/absolute/image.png)",
      "",
      "<svg><script>window.__wiki125Xss = true</script></svg>",
      "",
      "```mermaid",
      "this is not a mermaid diagram",
      "```",
      "",
      "```mermaid",
      "flowchart TD",
      "  A[Start] --> B[End]",
      "```",
    ].join("\n"),
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-125", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } });

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
              layout: { kind: "pane", id: "pane-1", path: "nested/rendering.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/nested/rendering.md`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".markdown-preview-view");
    await page.locator('.markdown-preview-view img[alt="relative"]').waitFor({ state: "visible" });
    await page.locator('.markdown-preview-view img[alt="parent"]').waitFor({ state: "visible" });
    await page.locator('.markdown-preview-view img[alt="image.png"]').waitFor({ state: "visible" });

    const images = await page.locator(".markdown-preview-view img").evaluateAll((elements) =>
      elements.map((element) => ({
        alt: element.getAttribute("alt"),
        src: element.getAttribute("src"),
        width: element.style.width,
      })),
    );
    const relative = images.find((image) => image.alt === "relative");
    const parent = images.find((image) => image.alt === "parent");
    const embed = images.find((image) => image.alt === "image.png");
    const external = images.find((image) => image.alt === "external");
    const scheme = images.find((image) => image.alt === "scheme");
    const absolute = images.find((image) => image.alt === "absolute");
    assert(relative?.src === "/api/vault/assets/nested/image.png", "relative image did not resolve note-relative");
    assert(parent?.src === "/api/vault/assets/attachments/parent.png", "parent-relative image did not resolve");
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.markdown-preview-view img[alt="fallback"]')].some(
        (image) => image.getAttribute("src") === "/api/vault/assets/fallback.png",
      ),
    );
    const fallbackSrc = await page.locator('.markdown-preview-view img[alt="fallback"]').getAttribute("src");
    assert(fallbackSrc === "/api/vault/assets/fallback.png", `fallback image did not resolve from vault root: ${fallbackSrc}`);
    assert(embed?.src === "/api/vault/assets/nested/image.png", "Obsidian embed did not resolve note-relative");
    assert(embed?.width === "300px", "Obsidian embed width modifier was not applied");
    assert(external?.src === "https://example.com/image.png", "external image URL was rewritten");
    assert(scheme?.src === "//cdn.example.com/image.png", "scheme-relative image URL was rewritten");
    assert(absolute?.src === "/absolute/image.png", "absolute image URL was rewritten");

    await page.locator(".markdown-mermaid-error").waitFor({ state: "visible" });
    assert(
      (await page.locator(".markdown-mermaid-error").innerText()).includes("this is not a mermaid diagram"),
      "malformed Mermaid source was not shown in the error fallback",
    );
    await page.waitForFunction(() => !document.querySelector('body [id^="dwiki-note-mermaid-"]'));
    const scratchIds = await page.locator('body [id^="dwiki-note-mermaid-"]').evaluateAll((elements) =>
      elements.map((element) => element.id),
    );
    assert(scratchIds.length === 0, `malformed Mermaid left scratch containers in document.body: ${scratchIds.join(", ")}`);
    await page.locator(".markdown-mermaid svg").waitFor({ state: "visible" });
    assert(await page.locator(".markdown-mermaid-error svg").count() === 0, "error fallback rendered outside its diagram container");

    assert(await page.locator(".markdown-preview-view > svg").count() === 0, "raw inline SVG was not escaped");
    assert((await page.locator(".markdown-preview-view").innerText()).includes("<svg>"), "escaped SVG source was not visible");
    assert((await page.evaluate(() => window.__wiki125Xss === true)) === false, "raw SVG script executed");

    const initialDiagram = await page.locator(".markdown-mermaid svg").innerHTML();
    const themeButton = page.locator('button[aria-label^="Switch to"]');
    await themeButton.waitFor({ state: "visible" });
    await themeButton.click();
    await page.locator(".markdown-mermaid svg").waitFor({ state: "visible" });
    assert(await page.locator(".markdown-mermaid svg").innerHTML() !== initialDiagram, "Mermaid did not rerender after theme change");
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
