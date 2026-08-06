// WIKI-244 review round 5 (M1): the serial `&&` test chain short-circuited
// at the first failure, so any earlier stage (build, pretest suites, unit
// batch, an accepted-red legacy suite) masked every stage behind it. This
// runner executes EVERY stage sequentially, streams each stage's output,
// records each exit status, prints an aggregate summary, and exits nonzero
// only AFTER all stages have run.
//
// Flags (used by the runner regression test):
//   --only=name1,name2         run only the named stages, in list order
//   --inject-fail-before=name  insert a deliberately failing stage before
//                              the named stage
import { spawnSync } from "node:child_process";

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

const injectBefore = parseFlag("inject-fail-before");
if (injectBefore) {
  const index = plan.findIndex((stage) => stage.name === injectBefore);
  if (index < 0) {
    console.error(`--inject-fail-before: stage not in plan: ${injectBefore}`);
    process.exit(2);
  }
  plan = [
    ...plan.slice(0, index),
    { name: "injected-failure", cmd: ["node", "-e", "console.error('injected stage failure'); process.exit(1)"] },
    ...plan.slice(index),
  ];
}

const results = [];
for (const stage of plan) {
  console.log(`\n=== stage start: ${stage.name} ===`);
  const started = Date.now();
  const outcome = spawnSync(stage.cmd[0], stage.cmd.slice(1), {
    stdio: "inherit",
    env: process.env,
  });
  const code = outcome.status ?? 1;
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  results.push({ name: stage.name, code, seconds });
  console.log(`=== stage end: ${stage.name} exit=${code} (${seconds}s) ===`);
}

const failed = results.filter((result) => result.code !== 0);
console.log("\n=== stage summary ===");
for (const result of results) {
  console.log(`${result.code === 0 ? "pass" : "FAIL"}  ${result.name} (exit=${result.code}, ${result.seconds}s)`);
}
console.log(`=== ${results.length - failed.length}/${results.length} stages passed ===`);
process.exit(failed.length > 0 ? 1 : 0);
