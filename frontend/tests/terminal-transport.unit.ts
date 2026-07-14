import test from "node:test";
import assert from "node:assert/strict";

import { terminalInputFrame } from "../src/terminal-transport.ts";

test("legacy peers receive JSON input until binary capability is advertised", () => {
  const frame = terminalInputFrame("\u0003", false);
  assert.equal(typeof frame, "string");
  assert.deepEqual(JSON.parse(frame), { type: "input", data: "\u0003" });
});

test("binary-capable peers receive UTF-8 input bytes", () => {
  const frame = terminalInputFrame("é\u0003", true);
  assert.ok(frame instanceof Uint8Array);
  assert.deepEqual([...frame], [0xc3, 0xa9, 0x03]);
});
