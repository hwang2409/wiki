// WIKI-244: capture the OpenCode-style transcript (always-visible tool rows,
// tree connectors, inline thinking, sub-agent trace), the block vim cursor in
// both composer modes, and the arbitrary font-weight settings controls. The
// same deterministic fixture runs against main (before) and the branch
// (after) so the PR evidence is comparable.
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const FRONTEND_ROOT = process.env.WIKI_FRONTEND_ROOT || process.cwd();
const harness = await import(
  pathToFileURL(path.resolve(FRONTEND_ROOT, "scripts", "wiki32-harness.mjs")).href
);
const { makeFixtureRoot, startBackend, writeQueue, writeRegistry } = harness;

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-244-evidence";
const TICKET = "WIKI-244";
const SUBAGENT_PROMPT =
  "Find signup/registration code. Report controllers, actions, validation rules, and tests that cover the register flow.";

function ts(second) {
  return `2026-08-05T12:00:${String(second).padStart(2, "0")}.000Z`;
}

function assistant(second, content) {
  return {
    type: "assistant",
    timestamp: ts(second),
    message: { role: "assistant", content },
  };
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

const LONG_OUTPUT = [
  "app/Actions/Fortify/CreateNewUser.php",
  ...Array.from({ length: 70 }, (_, i) =>
    `app/Providers/generated/Provider${String(i + 1).padStart(2, "0")}.php: registered`,
  ),
].join("\n");

const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  { type: "custom-title", customTitle: `${TICKET} opencode transcript`, sessionId: `fixture-${TICKET}` },
  {
    type: "user",
    timestamp: ts(0),
    message: {
      role: "user",
      content: "add a new field for signups that validates that they are 18 years old",
    },
  },
  assistant(1, [
    {
      type: "thinking",
      thinking:
        "The registration flow likely lives in Fortify's CreateNewUser action. I should read the current validation rules first, then check the routes, then decide where the date-of-birth rule belongs. A sub-agent can map the wider registration surface while I read the core action.",
    },
    { type: "text", text: "I'll help you add an age verification field for signups. Let me first explore the codebase to understand the current signup structure." },
    { type: "tool_use", id: "toolu_read", name: "Read", input: { file_path: "app/Actions/Fortify/CreateNewUser.php" } },
  ]),
  toolResult(2, "toolu_read", [
    "public function create(array $input): User",
    "{",
    "    Validator::make($input, [",
    "        'name' => ['required', 'string', 'max:255'],",
    "        'email' => ['required', 'string', 'email', 'max:255'],",
    "        'password' => $this->passwordRules(),",
    "    ])->validate();",
    "}",
  ].join("\n")),
  assistant(3, [
    { type: "tool_use", id: "toolu_task", name: "Task", input: { description: "Explore signup code", prompt: SUBAGENT_PROMPT } },
  ]),
  assistant(4, [
    { type: "tool_use", id: "toolu_grep", name: "Grep", input: { pattern: "date_of_birth", path: "app" } },
    { type: "tool_use", id: "toolu_read2", name: "Read", input: { file_path: "routes/web.php" } },
  ]),
  toolResult(6, "toolu_read2", "Route::post('/register', [RegisteredUserController::class, 'store']);"),
  toolResult(7, "toolu_grep", "app/Models/User.php:21:        'date_of_birth',"),
  toolResult(8, "toolu_task", "Mapped the register flow: CreateNewUser action, fortify config, RegistrationTest coverage."),
  assistant(9, [
    { type: "thinking", thinking: "Sequential case next: run the test suite, expect one failure until the migration lands." },
    { type: "tool_use", id: "toolu_bash", name: "Bash", input: { command: "php artisan test --filter=RegistrationTest", description: "Run registration tests" } },
  ]),
  toolResult(10, "toolu_bash", LONG_OUTPUT),
  assistant(11, [
    { type: "tool_use", id: "toolu_fail", name: "Bash", input: { command: "php artisan migrate --pretend", description: "Preview migration" } },
  ]),
  toolResult(12, "toolu_fail", "SQLSTATE[42S02]: Base table or view not found: users_dob", true),
  assistant(13, [
    { type: "text", text: "The migration preview surfaced a missing table alias — I'll fix the migration, add the `date_of_birth` rule with a `before_or_equal:-18 years` constraint, and rerun the suite." },
  ]),
];

const SUBAGENT_EVENTS = [
  { type: "user", timestamp: ts(3), message: { role: "user", content: SUBAGENT_PROMPT } },
  assistant(4, [
    { type: "tool_use", id: "sub_grep", name: "Grep", input: { pattern: "CreateNewUser|RegisterController|registration|signup" } },
  ]),
  toolResult(4, "sub_grep", "app/Actions/Fortify/CreateNewUser.php"),
  assistant(5, [
    { type: "tool_use", id: "sub_glob", name: "Glob", input: { pattern: "app/Actions/Fortify/**" } },
  ]),
  toolResult(5, "sub_glob", "app/Actions/Fortify/CreateNewUser.php\napp/Actions/Fortify/PasswordValidationRules.php"),
  assistant(6, [
    { type: "tool_use", id: "sub_read1", name: "Read", input: { file_path: "app/Actions/Fortify/CreateNewUser.php" } },
  ]),
  toolResult(6, "sub_read1", "…"),
  assistant(7, [
    { type: "tool_use", id: "sub_read2", name: "Read", input: { file_path: "tests/Feature/Auth/RegistrationTest.php" } },
  ]),
  toolResult(7, "sub_read2", "…"),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function preparePage(context) {
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
  return page;
}

async function settle(page) {
  await page.waitForSelector(".session-scroll", { state: "attached", timeout: 30_000 });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
}

// Pre-WIKI-244 builds collapse activity groups and tool bodies by default;
// open everything so before/after screenshots compare the same content.
async function openLegacyDisclosures(page) {
  for (let round = 0; round < 8; round += 1) {
    let clicked = 0;
    for (const selector of [
      ".session-activity-head[aria-expanded='false']",
      ".session-tool-head[aria-expanded='false']",
    ]) {
      for (const head of await page.locator(selector).all()) {
        await head.scrollIntoViewIfNeeded().catch(() => {});
        await head.click({ force: true }).catch(() => {});
        clicked += 1;
      }
    }
    if (clicked === 0) break;
    await page.waitForTimeout(150);
  }
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-244-");
  const transcript = path.join(fixtures.root, "wiki-244-claude.jsonl");
  await writeJsonl(transcript, TRANSCRIPT);
  const subDir = path.join(fixtures.root, "wiki-244-claude", "subagents");
  await fs.mkdir(subDir, { recursive: true });
  // Subagent ids must be 8-24 hex chars (backend SUBAGENT_ID_PATTERN).
  await writeJsonl(path.join(subDir, "agent-abcd1234.jsonl"), SUBAGENT_EVENTS);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  try {
    const browser = await chromium.launch({ headless: true });
    try {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1400 } });
      const page = await preparePage(context);
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await settle(page);
      await openLegacyDisclosures(page);
      await page.waitForTimeout(200);

      await page.screenshot({ path: path.join(OUT_DIR, "01-transcript-tail.png"), fullPage: true });

      // The transcript is virtualized and pinned to the bottom; walk back to
      // the top and capture the head of the conversation too.
      await page.evaluate(() => {
        const scroller = document.querySelector(".session-scroll");
        if (scroller) scroller.scrollTop = 0;
      });
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(OUT_DIR, "02-transcript-head.png") });

      const subtrace = page.locator(".session-subtrace").first();
      if (await subtrace.count()) {
        await subtrace.scrollIntoViewIfNeeded().catch(() => {});
        await page.waitForTimeout(200);
        await page.screenshot({ path: path.join(OUT_DIR, "02b-subagent-trace.png") }).catch(() => {});
      }

      // --- vim cursor: insert (typing) vs normal (Esc) ---
      const composer = page.locator(".session-composer textarea").first();
      await composer.click();
      await composer.pressSequentially("steer the worker toward the migration fix", { delay: 5 });
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("ArrowLeft");
      await page.waitForTimeout(150);
      const composerBox = page.locator(".session-composer").first();
      await composerBox.screenshot({ path: path.join(OUT_DIR, "03-vim-insert-cursor.png") });
      await page.keyboard.press("Escape");
      await page.waitForTimeout(150);
      await composerBox.screenshot({ path: path.join(OUT_DIR, "04-vim-normal-cursor.png") });
      await page.keyboard.press("Escape");

      // --- settings: font rows + weight input ---
      await page.locator('[aria-label="Settings"]').first().click();
      await page.locator(".settings-modal").waitFor({ state: "visible" });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(600);
      await page.locator(".settings-modal").screenshot({ path: path.join(OUT_DIR, "05-settings.png") });

      const monoWeight = page.getByLabel(/Monospace font weight/).first();
      if (await monoWeight.count()) {
        const tag = await monoWeight.evaluate((el) => el.tagName.toLowerCase());
        if (tag === "input") {
          await monoWeight.fill("650");
          await monoWeight.blur();
          await page.waitForTimeout(300);
          await page.locator(".settings-modal").screenshot({ path: path.join(OUT_DIR, "06-settings-weight-650.png") });
        }
      }

      const files = await fs.readdir(OUT_DIR);
      console.log(`[wiki-244] screenshots written to ${OUT_DIR}:`);
      for (const f of files.sort()) console.log(`  ${f}`);
    } finally {
      await browser.close();
    }
  } finally {
    if (typeof backend.stop === "function") await backend.stop();
    else if (typeof backend.kill === "function") backend.kill();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
