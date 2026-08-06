// WIKI-244 runner regressions (R5 M1, R6 M1/M2):
//  1. accepted red + NEW red → only the new red fails the gate; both are
//     classified separately; the [wiki-244] contracts still executed.
//  2. accepted red only → exit 0 (baseline does not gate) with XFAIL recorded.
//  3. hanging earlier stage → killed at its deadline, recorded as a timed-out
//     failure, and the run still reaches WIKI-244 and the final summary.
import { spawnSync } from "node:child_process";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function runRunner(args, env = {}) {
  const outcome = spawnSync("node", ["scripts/run-test-stages.mjs", ...args], {
    encoding: "utf-8",
    env: { ...process.env, ...env },
  });
  return { status: outcome.status, output: `${outcome.stdout ?? ""}\n${outcome.stderr ?? ""}` };
}

console.error("[wiki-244-runner] case 1: accepted red + new red — only the new red gates");
{
  const { status, output } = runRunner([
    "--only=wiki-244-opencode-transcript",
    "--inject-fail-before=wiki-244-opencode-transcript",
    "--inject-accepted-fail-before=wiki-244-opencode-transcript",
  ]);
  assert(status !== 0, `run with a new red must fail, got exit=${status}`);
  assert(/FAIL\s+injected-failure/.test(output), "new red must classify as FAIL");
  assert(/XFAIL\s+injected-accepted-failure/.test(output), "accepted red must classify as XFAIL");
  assert(
    output.includes("[wiki-244] all WIKI-244 contract checks passed"),
    "the WIKI-244 contracts must still execute after earlier failures",
  );
  assert(/pass\s+wiki-244-opencode-transcript/.test(output), "contract suite must be recorded as passed");
  assert(/unexpected failures: injected-failure/.test(output), "only the new red may drive the gate");
}

console.error("[wiki-244-runner] case 2: accepted red only — baseline does not gate");
{
  const { status, output } = runRunner([
    "--only=wiki-244-opencode-transcript",
    "--inject-accepted-fail-before=wiki-244-opencode-transcript",
  ]);
  assert(status === 0, `accepted-baseline failures must not gate, got exit=${status}`);
  assert(/XFAIL\s+injected-accepted-failure/.test(output), "accepted red must be recorded as XFAIL");
  assert(
    output.includes("[wiki-244] all WIKI-244 contract checks passed"),
    "the WIKI-244 contracts must execute in the accepted-only run",
  );
}

console.error("[wiki-244-runner] case 3: hanging stage is killed and the run continues");
{
  const { status, output } = runRunner([
    "--only=wiki-244-opencode-transcript",
    "--inject-hang-before=wiki-244-opencode-transcript",
  ]);
  assert(status !== 0, `a timed-out unexpected stage must fail the run, got exit=${status}`);
  assert(output.includes("injected hang started"), "the hanging stage must have started");
  assert(/FAIL\s+injected-hang \(exit=timeout/.test(output), "the hang must record a timed-out failure");
  assert(
    output.includes("[wiki-244] all WIKI-244 contract checks passed"),
    "the run must reach WIKI-244 after killing the hung stage",
  );
  assert(output.includes("=== stage summary ==="), "the final summary must print after a hang");
}

console.error("[wiki-244-runner] runner regression checks passed");
