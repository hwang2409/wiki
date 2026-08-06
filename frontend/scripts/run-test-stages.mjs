// WIKI-244 (R5 M1, R6 M1/M2): aggregating test-stage runner.
//
// - Every stage runs sequentially regardless of earlier failures; per-stage
//   results stream and a final summary prints after ALL stages ran.
// - Stages in the accepted baseline classify as XFAIL (accepted, failed) or
//   XPASS (accepted, passed); everything else is pass/FAIL. The runner exits
//   nonzero ONLY for unexpected failures, so automation can tell a new
//   regression from the accepted-red baseline.
// - Each stage runs under a generous deadline (default 10 minutes,
//   WIKI_TEST_STAGE_TIMEOUT_MS overrides). On expiry the stage's process
//   group is killed, the stage records a timed-out failure, and the run
//   continues — a hung stage can no longer mask the stages behind it.
//
// Flags (used by tests/wiki-244-runner-regression.mjs):
//   --only=name1,name2                 run only the named stages, in order
//   --inject-fail-before=name          insert a failing UNEXPECTED stage
//   --inject-accepted-fail-before=name insert a failing ACCEPTED stage
//   --inject-hang-before=name          insert a hanging stage (3s deadline)
import { spawn } from "node:child_process";

const UNIT_TESTS = [
  "tests/session-performance.unit.mjs",
  "tests/wiki32-harness.unit.mjs",
  "tests/terminal-transport.unit.ts",
  "tests/file-workspaces.test.ts",
  "tests/dashboard-logic.unit.ts",
  "tests/diff-parser.test.ts",
  "tests/visual-diff.test.ts",
  "tests/pdfjs-runtime.test.ts",
  "tests/pdf-nav.test.ts",
  "tests/plot-interaction.test.ts",
  "tests/plot-interaction-compile.test.ts",
];

const PLAYWRIGHT_SUITES = [
  // formerly npm pretest
  "terminal-pane",
  "wiki-240-run-header",
  // WIKI-244 contract suite — required stage, runs before the legacy chain
  "wiki-244-opencode-transcript",
  // legacy chain (original order)
  "wiki-237-prompt-dock",
  "wiki-94-session-performance",
  "native-transcript-surfaces",
  "ask-user-question-multi-select",
  "wiki59-shiki",
  "wiki87-code-overflow",
  "session-model-footer",
  "replace-agent-modal",
  "wiki-128-code-density",
  "dead-archive",
  "wiki-85-artifacts",
  "wiki-92-artifact-panel",
  "wiki-89-artifact-callability",
  "wiki-93-optimistic-send",
  "wiki-96-composer-reconcile",
  "wiki-97-transcript-markdown",
  "wiki-98-raw-html",
  "wiki-105-font-weights",
  "wiki-108-sidebar-pages",
  "wiki-109-palette-sessions",
  "wiki-114-mermaid-compact",
  "wiki-116-render-feedback",
  "wiki-124-large-svg",
  "wiki-125-note-markdown",
  "wiki-141-latex",
  "wiki-133-jk-scroll",
  "wiki-143-tokens",
  "wiki-143-badges",
  "wiki-143-diff",
  "wiki-144-badges",
  "wiki-145-layout",
  "wiki-145-noise",
  "wiki-145-typography",
  "wiki-146-palette",
  "wiki-147-unread-dot",
  "wiki-147-coalesce-followup",
  "wiki-148-slash-menu",
  "wiki-149-copy-diff",
  "wiki-153-transcript-polish",
  "wiki-154-agents-cold-start",
  "wiki-154-workspace-readiness",
  "wiki-151-nav-ia",
  "wiki-152-chrome",
  "wiki-157-utility-pages",
  "wiki-161-synthetic-source",
  "wiki-188-pdf-artifact",
  "wiki-189-markdown-image-cls",
  "wiki-218-terminal-chrome",
  "wiki-195-inspector",
  "wiki-194-plot-interaction",
  "wiki-234-opencode-restyle",
  "wiki-238-semantic-activity",
];

// Accepted-red baseline: union of the pre-existing red list enumerated in
// PR #168 and the environment-dependent reds observed in full runs on this
// machine. A failure here is XFAIL (recorded, does not gate); a failure
// anywhere else fails the run.
const ACCEPTED_FAILURES = new Set([
  "dead-archive",
  "replace-agent-modal",
  "wiki-85-artifacts",
  "wiki-89-artifact-callability",
  "wiki-92-artifact-panel",
  "wiki-93-optimistic-send",
  "wiki-108-sidebar-pages",
  "wiki-109-palette-sessions",
  "wiki-116-render-feedback",
  "wiki-124-large-svg",
  "wiki-143-tokens",
  "wiki-144-badges",
  "wiki-148-slash-menu",
  "wiki-154-agents-cold-start",
  "wiki-154-workspace-readiness",
  "wiki-157-utility-pages",
  "wiki-194-plot-interaction",
  "wiki-195-inspector",
  "wiki-218-terminal-chrome",
  "injected-accepted-failure",
]);

const DEFAULT_STAGE_TIMEOUT_MS = (() => {
  const raw = Number(process.env.WIKI_TEST_STAGE_TIMEOUT_MS);
  return Number.isFinite(raw) && raw > 0 ? raw : 600_000;
})();

const stages = [
  { name: "build", cmd: ["npm", "run", "build"] },
  { name: "unit", cmd: ["node", "--experimental-strip-types", "--test", ...UNIT_TESTS] },
  { name: "vitest", cmd: ["npx", "vitest", "run"] },
  ...PLAYWRIGHT_SUITES.map((suite) => ({
    name: suite,
    cmd: ["node", `tests/${suite}.playwright.mjs`],
  })),
];

// The runner's own no-short-circuit regression test runs as a stage right
// after the contract suite (it re-invokes this runner with --only, so it
// never recurses into the full plan).
{
  const anchor = stages.findIndex((stage) => stage.name === "wiki-244-opencode-transcript");
  stages.splice(anchor + 1, 0, {
    name: "wiki-244-runner-regression",
    cmd: ["node", "tests/wiki-244-runner-regression.mjs"],
  });
}

function parseFlag(name) {
  const prefix = `--${name}=`;
  const raw = process.argv.find((arg) => arg.startsWith(prefix));
  return raw ? raw.slice(prefix.length) : null;
}

let plan = stages;
const only = parseFlag("only");
if (only) {
  const wanted = only.split(",").map((name) => name.trim()).filter(Boolean);
  const byName = new Map(stages.map((stage) => [stage.name, stage]));
  const unknown = wanted.filter((name) => !byName.has(name));
  if (unknown.length) {
    console.error(`unknown stage(s): ${unknown.join(", ")}`);
    process.exit(2);
  }
  plan = wanted.map((name) => byName.get(name));
}

function injectStage(flag, stage) {
  const before = parseFlag(flag);
  if (!before) return;
  const index = plan.findIndex((candidate) => candidate.name === before);
  if (index < 0) {
    console.error(`--${flag}: stage not in plan: ${before}`);
    process.exit(2);
  }
  plan = [...plan.slice(0, index), stage, ...plan.slice(index)];
}

injectStage("inject-fail-before", {
  name: "injected-failure",
  cmd: ["node", "-e", "console.error('injected stage failure'); process.exit(1)"],
});
injectStage("inject-accepted-fail-before", {
  name: "injected-accepted-failure",
  cmd: ["node", "-e", "console.error('injected accepted failure'); process.exit(1)"],
});
injectStage("inject-hang-before", {
  name: "injected-hang",
  cmd: ["node", "-e", "console.error('injected hang started'); setInterval(() => {}, 1000)"],
  timeoutMs: 3_000,
});

function runStage(stage) {
  const timeoutMs = stage.timeoutMs ?? DEFAULT_STAGE_TIMEOUT_MS;
  return new Promise((resolve) => {
    // detached → own process group, so a timeout kill takes the stage's
    // children (spawned backends, browsers) with it.
    const child = spawn(stage.cmd[0], stage.cmd.slice(1), {
      stdio: "inherit",
      env: process.env,
      detached: true,
    });
    let timedOut = false;
    let settled = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        try { child.kill("SIGKILL"); } catch { /* already gone */ }
      }
    }, timeoutMs);
    const settle = (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code: timedOut ? "timeout" : code });
    };
    child.on("exit", (code, signal) => settle(code ?? (signal ? 1 : 0)));
    child.on("error", () => settle(1));
  });
}

function classify(name, code) {
  const failed = code !== 0;
  const accepted = ACCEPTED_FAILURES.has(name);
  if (!failed) return accepted ? "XPASS" : "pass";
  return accepted ? "XFAIL" : "FAIL";
}

const results = [];
for (const stage of plan) {
  console.log(`\n=== stage start: ${stage.name} ===`);
  const started = Date.now();
  const { code } = await runStage(stage);
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  const verdict = classify(stage.name, code);
  results.push({ name: stage.name, code, seconds, verdict });
  console.log(`=== stage end: ${stage.name} exit=${code} verdict=${verdict} (${seconds}s) ===`);
}

const unexpected = results.filter((result) => result.verdict === "FAIL");
const xfails = results.filter((result) => result.verdict === "XFAIL");
const xpasses = results.filter((result) => result.verdict === "XPASS");
console.log("\n=== stage summary ===");
for (const result of results) {
  console.log(`${result.verdict.padEnd(5)}  ${result.name} (exit=${result.code}, ${result.seconds}s)`);
}
console.log(
  `=== ${results.length} stages: ${results.filter((r) => r.verdict === "pass").length} pass, `
  + `${xpasses.length} XPASS (accepted red now passing), `
  + `${xfails.length} XFAIL (accepted baseline), `
  + `${unexpected.length} FAIL (unexpected) ===`,
);
if (unexpected.length) {
  console.log(`unexpected failures: ${unexpected.map((result) => result.name).join(", ")}`);
}
process.exit(unexpected.length > 0 ? 1 : 0);
