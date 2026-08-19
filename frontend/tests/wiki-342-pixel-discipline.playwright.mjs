import fs from "node:fs/promises";
import { chromium } from "playwright";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;

try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const styles = await fs.readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  await page.setContent(`<style>${styles}</style>`);

  const result = await page.evaluate(() => {
    const roots = [];
    const add = (className, tagName = "div") => {
      const node = document.createElement(tagName);
      node.className = className;
      node.textContent = "mono sample";
      document.body.append(node);
      roots.push(node);
      return node;
    };

    const markdown = add("markdown-preview-view");
    const markdownPre = document.createElement("pre");
    markdownPre.textContent = "const value = 1;";
    markdown.append(markdownPre);

    const shiki = add("shiki-block");
    const shikiPre = document.createElement("pre");
    shikiPre.textContent = "const value = 1;";
    shiki.append(shikiPre);

    const sessionRow = add("session-virtual-row");
    const sessionLog = add("session-pane-log", "pre");
    sessionRow.append(sessionLog);
    const codeFile = add("code-file-source", "pre");
    const terminal = add("terminal-pane-host");
    const terminalText = add("terminal-pane-notice");
    terminal.append(terminalText);

    const diff = add("diff-view");
    const diffBody = document.createElement("div");
    diffBody.className = "diff-hunk-body";
    const diffCode = document.createElement("code");
    diffCode.textContent = "+ const value = 1;";
    diffBody.append(diffCode);
    diff.append(diffBody);

    const transcript = add("transcript-preview-body");
    const transcriptPre = document.createElement("pre");
    transcriptPre.textContent = "output";
    transcript.append(transcriptPre);

    const fleet = add("fleet-screencast-tape");
    const fleetText = document.createElement("span");
    fleetText.className = "fleet-screencast-text";
    fleetText.textContent = "output";
    fleet.append(fleetText);

    const commandBackdrop = add("modal-backdrop command-palette-backdrop");
    const commandPalette = document.createElement("div");
    commandPalette.className = "quick-switcher command-palette";
    const commandTime = document.createElement("span");
    commandTime.className = "command-palette-time";
    commandTime.textContent = "now";
    commandPalette.append(commandTime);
    commandBackdrop.append(commandPalette);

    const transcriptHead = add("transcript-preview-head");
    transcriptHead.classList.add("is-expanded");
    const transcriptLabel = document.createElement("span");
    transcriptLabel.className = "transcript-preview-label";
    transcriptLabel.textContent = "output";
    transcriptHead.append(transcriptLabel);

    const copyPill = add("copy-pill", "button");
    const copyText = document.createElement("span");
    copyText.textContent = "copy";
    copyPill.append(copyText);

    const lightbox = add("artifact-lightbox");
    const lightboxTopbar = document.createElement("div");
    lightboxTopbar.className = "artifact-lightbox-topbar";
    lightboxTopbar.textContent = "artifact";
    lightbox.append(lightboxTopbar);

    const surfaces = [
      ["markdown pre", markdownPre],
      ["shiki pre", shikiPre],
      ["session log", sessionLog],
      ["code file", codeFile],
      ["terminal host", terminal],
      ["terminal notice", terminalText],
      ["diff code", diffCode],
      ["transcript pre", transcriptPre],
      ["fleet tape", fleet],
      ["command time", commandTime],
      ["transcript label", transcriptLabel],
      ["copy pill", copyText],
      ["lightbox title", lightboxTopbar],
    ];

    const triggers = [];
    const lineHeights = surfaces.map(([name, node]) => {
      const style = getComputedStyle(node);
      let ancestor = node.parentElement;
      while (ancestor !== null) {
        const ancestorStyle = getComputedStyle(ancestor);
        const opacity = Number.parseFloat(ancestorStyle.opacity);
        const transform = ancestorStyle.transform || "none";
        const filter = ancestorStyle.filter || "none";
        const backdropFilter = ancestorStyle.backdropFilter || "none";
        const webkitBackdropFilter = ancestorStyle.webkitBackdropFilter || "none";
        const willChange = ancestorStyle.willChange || "auto";
        const contain = ancestorStyle.contain || "none";
        const trigger =
          transform !== "none" ||
          filter !== "none" ||
          backdropFilter !== "none" ||
          webkitBackdropFilter !== "none" ||
          (Number.isFinite(opacity) && opacity < 1) ||
          willChange !== "auto" ||
          contain.split(" ").includes("paint");
        if (trigger) {
          triggers.push(`${name}: ${ancestor.className || ancestor.tagName}`);
          break;
        }
        ancestor = ancestor.parentElement;
      }
      return [name, style.lineHeight];
    });

    const bodyStyle = getComputedStyle(document.body);
    roots.forEach((node) => node.remove());
    return { lineHeights, triggers, bodyZoom: bodyStyle.zoom };
  });

  assert(result.bodyZoom === "1", `body zoom expected 1, got ${result.bodyZoom}`);
  assert(result.triggers.length === 0, `mono ancestor triggers found: ${result.triggers.join(", ")}`);
  for (const [name, lineHeight] of result.lineHeights) {
    assert(/px$/u.test(lineHeight), `${name} line-height is not absolute: ${lineHeight}`);
    assert(Number.isInteger(Number.parseFloat(lineHeight)), `${name} line-height is fractional: ${lineHeight}`);
  }
} finally {
  if (browser) await browser.close();
}
