// WIKI-244 review round 1 contracts:
//  B1 — no control anywhere can hide the activity trace.
//  B2 — over-threshold thinking renders in full with no show-all gate.
//  B3 — sub-agent child traces render thinking + tool input/output blocks
//       through the shared row model, windowed with an explicit history note.
//  H2 — child polling uses cursor deltas: repeat polls carry cursor>0 and
//       return bounded (near-empty) payloads; the DOM row window stays capped.
//  M3 — the vim cursor is a block in insert AND normal mode: native caret
//       transparent, overlay geometry + covered char in insert, one-char
//       selection in normal, IME-safe key handling, no cursor after blur.
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-244";
const SUBAGENT_PROMPT =
  "Audit the registration flow end to end. Enumerate controllers, validation rules, and the tests that cover them.";
const SUBTRACE_MAX_ROWS = 60;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-244] ${message}`);
}

function ts(second) {
  const minutes = String(Math.floor(second / 60)).padStart(2, "0");
  const seconds = String(second % 60).padStart(2, "0");
  return `2026-08-05T12:${minutes}:${seconds}.000Z`;
}

function assistant(second, content) {
  return { type: "assistant", timestamp: ts(second), message: { role: "assistant", content } };
}

function toolResult(second, toolId, content, isError = false) {
  return {
    type: "user",
    timestamp: ts(second),
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: toolId, content, is_error: isError }],
    },
  };
}

const LONG_THINKING = Array.from(
  { length: 80 },
  (_, i) => `Reasoning line ${i + 1}: weigh the migration order against the validation rules before touching the schema.`,
).join("\n");

const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  {
    type: "user",
    timestamp: ts(0),
    message: { role: "user", content: "audit the signup flow and add the age gate" },
  },
  assistant(1, [
    { type: "thinking", thinking: LONG_THINKING },
    { type: "text", text: "Auditing now. I will delegate the wide sweep to a sub-agent." },
    { type: "tool_use", id: "toolu_read", name: "Read", input: { file_path: "app/Actions/Fortify/CreateNewUser.php" } },
  ]),
  toolResult(2, "toolu_read", "public function create(array $input): User { /* rules */ }"),
  // Running Task (no result) — SubagentTrace must poll while active.
  assistant(3, [
    { type: "tool_use", id: "toolu_task", name: "Task", input: { description: "Audit signup", prompt: SUBAGENT_PROMPT } },
  ]),
];

const CHILD_PAIRS = 250;
const SUBAGENT_EVENTS = [
  { type: "user", timestamp: ts(3), message: { role: "user", content: SUBAGENT_PROMPT } },
  assistant(4, [
    {
      type: "thinking",
      thinking: "Child reasoning: start from the fortify config, then walk every consumer of CreateNewUser.",
    },
  ]),
  assistant(5, [
    { type: "tool_use", id: "sub_bash", name: "Bash", input: { command: "php artisan route:list | grep register", description: "List register routes" } },
  ]),
  toolResult(5, "sub_bash", "POST /register ..... RegisteredUserController@store"),
  ...Array.from({ length: CHILD_PAIRS }, (_, i) => [
    assistant(6 + i, [
      { type: "tool_use", id: `sub_read_${i}`, name: "Read", input: { file_path: `app/generated/Consumer${String(i).padStart(3, "0")}.php` } },
    ]),
    toolResult(6 + i, `sub_read_${i}`, `Consumer${String(i).padStart(3, "0")} uses CreateNewUser`),
  ]).flat(),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-244-contract-");
  const transcript = path.join(fixtures.root, "wiki-244-claude.jsonl");
  await writeJsonl(transcript, TRANSCRIPT);
  const subDir = path.join(fixtures.root, "wiki-244-claude", "subagents");
  await fs.mkdir(subDir, { recursive: true });
  await writeJsonl(path.join(subDir, "agent-feed12345.jsonl"), SUBAGENT_EVENTS);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 1400 } });
    const page = await context.newPage();
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
    }, {
      layout: {
        version: 2,
        activeWindowId: "window-0",
        windows: [
          { id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` } },
        ],
      },
    });

    // H2 instrumentation: record every child-trace request/response.
    const subagentCalls = [];
    page.on("response", (response) => {
      const url = response.url();
      if (!url.includes("/subagents/")) return;
      const query = new URL(url).searchParams;
      const cursor = Number(query.get("cursor") ?? "-1");
      const limit = Number(query.get("limit") ?? "-1");
      subagentCalls.push(
        response.text().then((body) => ({ cursor, limit, bytes: body.length, events: (() => {
          try { return JSON.parse(body).events?.length ?? -1; } catch { return -1; }
        })() })).catch(() => ({ cursor, limit, bytes: -1, events: -1 })),
      );
    });

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-scroll", { timeout: 30_000 });
    await page.evaluate(() => document.fonts.ready);

    logStep("B1: no control can hide the trace");
    await page.locator(".session-activity").first().waitFor({ state: "visible" });
    const hidingControls = await page.locator(
      ".session-activity-head button, button.session-activity-head, .session-activity [aria-expanded]",
    ).count();
    assert(hidingControls === 0, `expected no disclosure controls in the trace, found ${hidingControls}`);
    const bodies = await page.locator(".session-activity .session-activity-body").count();
    assert(bodies > 0, "activity body must render unconditionally");

    logStep("B2: over-threshold thinking renders in full, no show-all");
    const thinking = page.locator(".session-thinking").first();
    await thinking.waitFor({ state: "visible" });
    const thinkingText = await thinking.innerText();
    assert(thinkingText.includes("Reasoning line 80"), "long thinking must be fully readable without clicks");
    const clampControls = await page.locator(".session-thinking .stream-clamp-toggle, .session-thinking .stream-clamp").count();
    assert(clampControls === 0, "thinking must not render through an interactive clamp");

    logStep("B3: child trace shows thinking + tool input/output blocks");
    const subtrace = page.locator(".session-subtrace").first();
    await subtrace.waitFor({ state: "visible", timeout: 20_000 });
    await subtrace.locator(".session-thinking, .session-activity-row").first().waitFor();
    const childRows = await subtrace.locator(".session-activity-row").count();
    assert(childRows > 0, "child trace must render trace rows");
    assert(childRows <= SUBTRACE_MAX_ROWS, `child rows must be windowed to ${SUBTRACE_MAX_ROWS}, got ${childRows}`);
    const moreNote = await subtrace.locator(".session-subtrace-more").innerText();
    assert(/\+\d+ earlier rows/.test(moreNote), `history window must be labelled, got: ${moreNote}`);
    const childOutputs = await subtrace.locator(".transcript-preview").count();
    assert(childOutputs > 0, "child tool outputs must render as visible blocks");
    const lastChildOutput = await subtrace.locator(".transcript-preview-body").last().innerText();
    assert(lastChildOutput.includes("uses CreateNewUser"), "child output content must be readable inline");

    logStep("H2: polling uses cursor deltas with bounded payloads");
    // Wait until at least three child polls have happened (~2 poll intervals).
    const deadline = Date.now() + 15_000;
    while (subagentCalls.length < 3 && Date.now() < deadline) {
      await page.waitForTimeout(400);
    }
    assert(subagentCalls.length >= 3, `expected >=3 child polls, saw ${subagentCalls.length}`);
    const calls = await Promise.all(subagentCalls);
    assert(calls[0].cursor === 0, `first poll must start at cursor 0, got ${calls[0].cursor}`);
    // H2 (round 2): the first fetch is bounded server-side — the request
    // carries a limit and the response holds at most that many events even
    // though the child transcript is larger (500+ events in this fixture).
    for (const call of calls) {
      assert(call.limit > 0 && call.limit <= 1000, `child polls must carry a bounded limit, got ${call.limit}`);
    }
    assert(calls[0].events > 0 && calls[0].events <= calls[0].limit,
      `first response must be capped at the limit (limit=${calls[0].limit}, events=${calls[0].events})`);
    const repeats = calls.slice(1);
    for (const call of repeats) {
      assert(call.cursor > 0, `repeat polls must carry a positive cursor, got ${call.cursor}`);
    }
    const idleRepeats = repeats.filter((call) => call.events === 0);
    assert(idleRepeats.length > 0, `idle repeat polls must return empty deltas: ${JSON.stringify(calls)}`);
    const maxRepeatBytes = Math.max(...repeats.map((call) => call.bytes));
    assert(
      maxRepeatBytes < Math.max(2_000, calls[0].bytes / 10),
      `repeat poll payloads must stay bounded (first=${calls[0].bytes}B, repeat max=${maxRepeatBytes}B)`,
    );

    logStep("M3: block cursor in insert and normal mode");
    const composer = page.locator(".session-composer textarea").first();
    const focusComposer = async () => {
      await composer.click();
      await page.waitForFunction(
        () => document.activeElement === document.querySelector(".session-composer textarea"),
      );
    };
    await focusComposer();
    await composer.evaluate((el) => { el.dataset.wiki244 = "tagged"; });
    const caretColor = await composer.evaluate((el) => getComputedStyle(el).caretColor);
    assert(caretColor === "rgba(0, 0, 0, 0)" || caretColor === "transparent",
      `native caret must be transparent, got ${caretColor}`);
    await composer.pressSequentially("steer the migration", { delay: 5 });
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.waitForTimeout(120);
    const overlay = page.locator(".session-empty-block-cursor");
    try {
      await overlay.waitFor({ state: "visible", timeout: 5000 });
    } catch {
      // Focus can be stolen by a late layout pass under load — refocus once.
      await focusComposer();
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("ArrowRight");
      await overlay.waitFor({ state: "visible", timeout: 10_000 });
    }
    const overlayGeometry = () => page.evaluate(() => {
      const cursor = document.querySelector(".session-empty-block-cursor");
      const input = document.querySelector(".session-composer textarea");
      if (!cursor || !input) return null;
      const c = cursor.getBoundingClientRect();
      const t = input.getBoundingClientRect();
      return {
        within: c.left >= t.left - 1 && c.right <= t.right + 1 && c.top >= t.top - 1 && c.bottom <= t.bottom + 1,
        width: c.width,
        height: c.height,
        char: cursor.textContent,
      };
    });
    const insertGeom = await overlayGeometry();
    assert(insertGeom.within, "insert block cursor must sit inside the textarea");
    assert(insertGeom.width > 3 && insertGeom.height > 10,
      `insert block cursor must have block geometry, got ${insertGeom.width}x${insertGeom.height}`);
    assert(insertGeom.char === "i", `insert block must cover the char under the caret, got "${insertGeom.char}"`);

    await page.keyboard.press("Escape");
    await page.waitForTimeout(120);
    const normalState = await composer.evaluate((el) => ({
      vimClass: el.classList.contains("is-vim-normal"),
      selection: (el.selectionEnd ?? 0) - (el.selectionStart ?? 0),
    }));
    assert(normalState.vimClass, "normal mode must apply the block-selection class");
    assert(normalState.selection === 1, `normal mode must select exactly the char under the cursor, got ${normalState.selection}`);

    logStep("M3: IME-safe key handling");
    const textBefore = await composer.inputValue();
    await composer.evaluate((el) => {
      const ev = new KeyboardEvent("keydown", { key: "x", bubbles: true, cancelable: true });
      Object.defineProperty(ev, "isComposing", { value: true });
      el.dispatchEvent(ev);
    });
    await page.waitForTimeout(80);
    const textAfter = await composer.inputValue();
    assert(textAfter === textBefore, "composing key events must not trigger vim commands");

    logStep("M1: overlay tracks textarea scroll and layout reflow");
    await page.keyboard.press("i");
    const longDraft = Array.from({ length: 40 }, (_, i) => `draft line ${i + 1}`).join("\n");
    await composer.fill(longDraft);
    const scrollable = await composer.evaluate((el) => el.scrollHeight > el.clientHeight);
    assert(scrollable, "capped draft must overflow the composer for the scroll assertions");
    // The polling waits re-assert the scroll position on every poll (a held
    // scroll position emits repeated scroll events for real users) and pass
    // only once the overlay reaches the expected state.
    const waitOverlayAtBottom = () => page.waitForFunction(() => {
      const el = document.querySelector(".session-composer textarea");
      if (!el || document.activeElement !== el) return false;
      el.setSelectionRange(el.value.length, el.value.length);
      el.scrollTop = el.scrollHeight;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
      return document.querySelectorAll(".session-empty-block-cursor").length === 1;
    }, undefined, { timeout: 15_000 });
    const waitOverlayHiddenAtTop = () => page.waitForFunction(() => {
      const el = document.querySelector(".session-composer textarea");
      if (!el) return false;
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
      return document.querySelectorAll(".session-empty-block-cursor").length === 0;
    }, undefined, { timeout: 15_000 });
    await waitOverlayAtBottom();
    assert((await overlayGeometry()).within, "block cursor must sit inside the textarea at the scrolled caret");
    // Scroll the caret line out of view: the block must hide, not float over
    // unrelated rows.
    await waitOverlayHiddenAtTop();
    await waitOverlayAtBottom();
    // Pane reflow: a narrower viewport rewraps the draft; the overlay must
    // recompute and stay inside the textarea.
    await page.setViewportSize({ width: 1060, height: 1400 });
    await page.waitForTimeout(300);
    const reflowWidth = await composer.evaluate((el) => el.clientWidth);
    assert(reflowWidth > 200, `reflow precondition: composer must stay usable, got ${reflowWidth}px`);
    await waitOverlayAtBottom();
    assert((await overlayGeometry()).within, "block cursor must stay inside the textarea after a pane reflow");

    logStep("M3: no cursor after blur");
    await composer.evaluate((el) => el.blur());
    await page.waitForTimeout(120);
    assert((await overlay.count()) === 0, "block cursor must disappear when the composer loses focus");

    logStep("all WIKI-244 contract checks passed");
  } finally {
    await browser.close();
    if (typeof backend.stop === "function") await backend.stop();
    else if (typeof backend.kill === "function") backend.kill();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
