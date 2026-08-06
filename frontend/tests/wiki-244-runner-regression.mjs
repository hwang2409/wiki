// WIKI-244 review round 5 (M1): the stage runner must not short-circuit.
// Inject a deliberately failing stage BEFORE the WIKI-244 contract suite and
// assert that (a) the [wiki-244] steps still executed and passed, and
// (b) the overall run still exits nonzero because of the injected failure.
import { spawnSync } from "node:child_process";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

console.error("[wiki-244-runner] running stage runner with an injected earlier failure");
const outcome = spawnSync(
  "node",
  [
    "scripts/run-test-stages.mjs",
    "--only=wiki-244-opencode-transcript",
    "--inject-fail-before=wiki-244-opencode-transcript",
  ],
  { encoding: "utf-8", env: process.env },
);

const output = `${outcome.stdout ?? ""}\n${outcome.stderr ?? ""}`;
assert(outcome.status !== 0, `overall run must fail because of the injected stage, got exit=${outcome.status}`);
assert(output.includes("injected stage failure"), "the injected stage must have run and failed");
assert(
  output.includes("[wiki-244] all WIKI-244 contract checks passed"),
  "the WIKI-244 contract suite must still execute after an earlier stage failure",
);
assert(
  /FAIL\s+injected-failure/.test(output),
  "the summary must record the injected failure",
);
assert(
  /pass\s+wiki-244-opencode-transcript/.test(output),
  "the summary must record the contract suite as passed",
);
console.error("[wiki-244-runner] runner regression checks passed");
