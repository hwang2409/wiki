// WIKI-271: content-based language fallback for headerless / path-less diffs.
// When a codex fileChange or truncated snapshot lands without a file name,
// languageForPath returns null; languageFromContent must still resolve common
// syntaxes so diff highlighting doesn't degrade to plain text.
import { describe, expect, test } from "vitest";
import { languageForPath, languageFromContent } from "../src/shiki";

describe("languageForPath", () => {
  test("resolves extension-driven aliases", () => {
    expect(languageForPath("frontend/src/session.tsx")).toBe("tsx");
    expect(languageForPath("backend/app/store.py")).toBe("python");
    expect(languageForPath("infra/Dockerfile")).toBe("docker");
  });

  test("unknown or missing extension yields null", () => {
    expect(languageForPath("unnamed")).toBeNull();
    expect(languageForPath("plans/schedule")).toBeNull();
  });
});

describe("languageFromContent", () => {
  test("shebang picks the interpreter language", () => {
    expect(languageFromContent("#!/usr/bin/env python3\nprint('hi')")).toBe("python");
    expect(languageFromContent("#!/bin/bash\nset -eu\necho ok")).toBe("bash");
    expect(languageFromContent("#!/usr/bin/env node\nconsole.log(1)")).toBe("javascript");
  });

  test("python imports and defs are detected", () => {
    const source = [
      "from dataclasses import dataclass",
      "",
      "def build(payload):",
      "    return payload",
    ].join("\n");
    expect(languageFromContent(source)).toBe("python");
  });

  test("typescript type/interface annotations resolve to typescript", () => {
    const source = [
      "import { useMemo } from 'react';",
      "",
      "interface Props { name: string; count: number }",
      "export const x: number = 1;",
    ].join("\n");
    expect(languageFromContent(source)).toBe("typescript");
  });

  test("plain ESM javascript falls to javascript", () => {
    const source = [
      "import { readFile } from 'node:fs/promises';",
      "",
      "const data = await readFile('a.txt', 'utf8');",
      "console.log(data);",
    ].join("\n");
    expect(languageFromContent(source)).toBe("javascript");
  });

  test("JSON payload is detected", () => {
    expect(languageFromContent('{"state":"working","pr":null}')).toBe("json");
    expect(languageFromContent('[{"a":1},{"b":2}]')).toBe("json");
  });

  test("markdown heading and pipe table are detected", () => {
    expect(languageFromContent("# Title\n\nProse below.")).toBe("markdown");
    expect(languageFromContent("| a | b |\n| --- | --- |\n| 1 | 2 |")).toBe("markdown");
  });

  test("SQL statement is detected", () => {
    expect(languageFromContent("SELECT id FROM caregivers WHERE org_id = $1;")).toBe("sql");
  });

  test("go and rust snippets resolve", () => {
    expect(languageFromContent("package main\n\nfunc main() {}\n")).toBe("go");
    expect(languageFromContent("fn main() {\n    println!(\"hi\");\n}\n")).toBe("rust");
  });

  test("ambiguous or empty content stays null", () => {
    expect(languageFromContent("")).toBeNull();
    expect(languageFromContent("hello there\nnot code")).toBeNull();
  });
});
