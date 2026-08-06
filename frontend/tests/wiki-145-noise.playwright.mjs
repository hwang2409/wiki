import { readFileSync } from "node:fs";
import path from "node:path";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const repoRoot = path.resolve(new URL(".", import.meta.url).pathname, "..");
const css = readFileSync(path.join(repoRoot, "src/styles.css"), "utf8");

function ruleBody(selector) {
  const anchor = css.indexOf(`${selector} {`);
  if (anchor === -1) return null;
  const end = css.indexOf("}", anchor);
  if (end === -1) return null;
  return css.slice(anchor, end);
}

// Offender 2: .workspace-sidebar must NOT be painted with background-secondary.
{
  const body = ruleBody(".workspace-sidebar");
  assert(body, ".workspace-sidebar rule missing");
  assert(
    !/background-color:\s*var\(--background-secondary\)/.test(body),
    "workspace-sidebar still fills with background-secondary — should be transparent",
  );
}

// Offender 3: .tree-item-self.is-active must NOT rely on filled background-modifier-active.
{
  const body = ruleBody(".tree-item-self.is-active");
  assert(body, ".tree-item-self.is-active rule missing");
  assert(
    !/background-color:\s*var\(--background-modifier-active\)/.test(body),
    "tree-item-self.is-active still fills with background-modifier-active — expected accent bar via box-shadow",
  );
  assert(
    /box-shadow:\s*inset\s+2px\s+0/.test(body),
    "tree-item-self.is-active expected inset 2px left accent bar",
  );
}

// Offender 4: WIKI-151 deleted the `.workspace-tab` rule outright — the
// phantom header bar is gone (bottom-rail tabs are canonical). Assert absence
// so a regression that revives the top tab band lands here.
{
  const body = ruleBody(".workspace-tab");
  assert(body === null, ".workspace-tab rule reintroduced — WIKI-151 removed the phantom tab header");
}

// Offender 5: .notice must NOT have full border + secondary fill.
{
  const body = ruleBody(".notice");
  assert(body, ".notice rule missing");
  assert(
    !/border:\s*1px\s+solid\s+var\(--background-modifier-border\)/.test(body),
    "notice still uses full 1px border — expected left accent bar only",
  );
  assert(
    !/background-color:\s*var\(--background-secondary\)/.test(body),
    "notice still filled with background-secondary — expected transparent",
  );
  assert(
    /border-left:\s*2px\s+solid/.test(body),
    "notice expected 2px left accent bar",
  );
}

// Offender 9: the per-event activity flow must not wear the old aggregate
// border+fill.
{
  const body = ruleBody(".session-activity");
  assert(body, ".session-activity rule missing");
  assert(
    !/border:\s*1px\s+solid\s+var\(--background-modifier-border\)/.test(body),
    "session-activity still bordered — expected plain flow wrapper",
  );
  assert(
    !/background-color:\s*var\(--background-secondary\)/.test(body),
    "session-activity still filled — expected transparent",
  );
}

// Offender 12: .session-header must NOT carry the border-bottom chrome band.
{
  const body = ruleBody(".session-header");
  assert(body, ".session-header rule missing");
  assert(
    !/border-bottom:\s*1px\s+solid\s+var\(--background-modifier-border\)/.test(body),
    "session-header still has border-bottom band — expected whitespace separator only",
  );
}

// Offender 14: .session-assistant must NOT carry left-border rail.
{
  const body = ruleBody(".session-assistant");
  assert(body, ".session-assistant rule missing");
  assert(
    !/border-left:\s*2px\s+solid/.test(body),
    "session-assistant still has left-border rail — expected plain flow",
  );
}

// Focus ring calmed to accent outline + offset (workstream E).
{
  const focusIdx = css.indexOf(":where(button, input, textarea, select, a, [role=\"button\"]):focus-visible");
  assert(focusIdx !== -1, "focus-visible :where rule missing");
  const end = css.indexOf("}", focusIdx);
  const body = css.slice(focusIdx, end);
  assert(
    !/box-shadow:\s*0\s+0\s+0\s+2px\s+var\(--background-primary\),\s*0\s+0\s+0\s+4px\s+var\(--text-normal\)/.test(body),
    "old double box-shadow focus ring still present — expected outline-based ring",
  );
  assert(
    /outline:\s*1\.5px\s+solid/.test(body),
    "expected outline: 1.5px solid on focus-visible",
  );
}

// Grid narrowed for calmer rail (workstream B).
{
  const gridIdx = css.indexOf(".app-container");
  const gridBlock = css.slice(gridIdx, gridIdx + 400);
  assert(
    !/grid-template-columns:\s*44px\s+280px/.test(gridBlock),
    "app-container still uses old 280px sidebar column",
  );
}

console.log("wiki-145-noise: all noise offenders confirmed removed");
