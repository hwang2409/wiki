import assert from "node:assert/strict";
import test from "node:test";
import {
  classifyEnumerated,
  isEnumeratedFamily,
  mergePools,
  registerInstalledFontFaces,
  resetFontEnumerationCacheForTests,
  synthesizedChoice,
  type InstalledFontFamily,
  type FontChoice,
  type MonoProbe,
} from "../src/font-enumeration.ts";

const CURATED_MONO: FontChoice[] = [
  { label: "JetBrains Mono", family: "JetBrains Mono", stack: '"JetBrains Mono", monospace' },
  { label: "Fira Code", family: "Fira Code", stack: '"Fira Code", monospace' },
];

const CURATED_PROP: FontChoice[] = [
  { label: "System", family: "-apple-system", stack: "-apple-system, sans-serif" },
  { label: "Inter", family: "Inter", stack: '"Inter", sans-serif' },
];

test("mergePools: curated fonts always come first, in original order", () => {
  const merged = mergePools({
    curated: CURATED_MONO,
    installed: [],
    synthesize: (family) => synthesizedChoice(family, true),
  });
  assert.deepEqual(merged.map((f) => f.label), ["JetBrains Mono", "Fira Code"]);
});

test("mergePools: enumerated families are appended after curated, in enumeration order", () => {
  const merged = mergePools({
    curated: CURATED_MONO,
    installed: ["Cascadia Code", "Anonymous Pro"],
    synthesize: (family) => synthesizedChoice(family, true),
  });
  assert.deepEqual(
    merged.map((f) => f.label),
    ["JetBrains Mono", "Fira Code", "Cascadia Code", "Anonymous Pro"],
  );
});

test("mergePools: curated family name skips the same enumerated entry (no duplicate)", () => {
  const merged = mergePools({
    curated: CURATED_MONO,
    installed: ["Fira Code", "Cascadia Code"],
    synthesize: (family) => synthesizedChoice(family, true),
  });
  assert.deepEqual(
    merged.map((f) => f.label),
    ["JetBrains Mono", "Fira Code", "Cascadia Code"],
  );
});

test("mergePools: JetBrains Mono and JetBrainsMono Nerd Font stay as separate entries", () => {
  // The Henry directive from WIKI-260: distinct family names must not fold.
  const merged = mergePools({
    curated: CURATED_MONO,
    installed: [
      "JetBrainsMono Nerd Font",
      "JetBrainsMono Nerd Font Mono",
      "JetBrainsMonoNL Nerd Font",
    ],
    synthesize: (family) => synthesizedChoice(family, true),
  });
  const labels = merged.map((f) => f.label);
  assert.ok(labels.includes("JetBrains Mono"));
  assert.ok(labels.includes("JetBrainsMono Nerd Font"));
  assert.ok(labels.includes("JetBrainsMono Nerd Font Mono"));
  assert.ok(labels.includes("JetBrainsMonoNL Nerd Font"));
  // Verify each stays as its own family in the CSS stack — no aliasing.
  const nerd = merged.find((f) => f.label === "JetBrainsMono Nerd Font");
  assert.equal(nerd?.family, "JetBrainsMono Nerd Font");
  assert.match(nerd?.stack ?? "", /^"JetBrainsMono Nerd Font"/);
});

test("mergePools: same enumerated family listed twice is not duplicated", () => {
  const merged = mergePools({
    curated: [],
    installed: ["Cascadia Code", "Cascadia Code"],
    synthesize: (family) => synthesizedChoice(family, true),
  });
  assert.deepEqual(merged.map((f) => f.family), ["Cascadia Code"]);
});

test("synthesizedChoice: label and family match the enumerated string exactly", () => {
  const choice = synthesizedChoice("JetBrainsMono Nerd Font Propo", false);
  assert.equal(choice.label, "JetBrainsMono Nerd Font Propo");
  assert.equal(choice.family, "JetBrainsMono Nerd Font Propo");
});

test("synthesizedChoice: mono tail vs prop tail flows through the stack", () => {
  const mono = synthesizedChoice("Cascadia Code", true);
  const prop = synthesizedChoice("Cascadia Code", false);
  assert.match(mono.stack, /monospace$/);
  assert.match(prop.stack, /sans-serif$/);
});

test("synthesizedChoice: family names with quotes are escaped", () => {
  const choice = synthesizedChoice('Weird "Quote" Font', false);
  assert.match(choice.stack, /^"Weird \\"Quote\\" Font"/);
});

test("classifyEnumerated: probe decides mono vs prop", () => {
  const mono = new Set(["JetBrains Mono", "Cascadia Code"]);
  const probe: MonoProbe = (family) => mono.has(family);
  const buckets = classifyEnumerated(
    ["JetBrains Mono", "Inter", "Cascadia Code", "Georgia"],
    probe,
  );
  assert.deepEqual(buckets.mono, ["JetBrains Mono", "Cascadia Code"]);
  assert.deepEqual(buckets.prop, ["Inter", "Georgia"]);
});

test("classifyEnumerated: empty enumerated list yields empty buckets", () => {
  const buckets = classifyEnumerated([], () => true);
  assert.deepEqual(buckets, { mono: [], prop: [] });
});

test("registerInstalledFontFaces: registers lazy faces and keeps enumerated families available", () => {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalFontFace = Object.getOwnPropertyDescriptor(globalThis, "FontFace");
  const added: Array<{ family: string; source: string; descriptors: Record<string, string> }> = [];
  const fakeDocument = {
    fonts: {
      add(face: { family: string; source: string; descriptors: Record<string, string> }) {
        added.push(face);
      },
    },
  };
  class FakeFontFace {
    family: string;
    source: string;
    descriptors: Record<string, string>;

    constructor(family: string, source: string, descriptors: Record<string, string>) {
      this.family = family;
      this.source = source;
      this.descriptors = descriptors;
    }
  }
  const fonts: InstalledFontFamily[] = [
    { family: "JetBrains Mono", files: [{ id: "jb-regular", weight: 400, style: "Regular" }] },
    { family: "Menlo", files: [{ id: "menlo", weight: 400 }] },
  ];
  try {
    Object.defineProperty(globalThis, "document", { value: fakeDocument, configurable: true });
    Object.defineProperty(globalThis, "FontFace", { value: FakeFontFace, configurable: true });
    resetFontEnumerationCacheForTests();
    const count = registerInstalledFontFaces(fonts, (family) => family === "Menlo");
    assert.equal(count, 1);
    assert.equal(added[0]?.family, "JetBrains Mono");
    assert.match(added[0]?.source ?? "", /\/api\/fonts\/file\/jb-regular/);
    assert.equal(added[0]?.descriptors.weight, "400");
    assert.equal(added[0]?.descriptors.style, "normal");
    assert.equal(isEnumeratedFamily("JetBrains Mono"), true);
    assert.equal(isEnumeratedFamily("Menlo"), true);
    assert.equal(registerInstalledFontFaces(fonts, (family) => family === "Menlo"), 0);
  } finally {
    resetFontEnumerationCacheForTests();
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete (globalThis as { document?: unknown }).document;
    if (originalFontFace) Object.defineProperty(globalThis, "FontFace", originalFontFace);
    else delete (globalThis as { FontFace?: unknown }).FontFace;
  }
});

test("registerInstalledFontFaces: registers every variable face sharing one file", () => {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalFontFace = Object.getOwnPropertyDescriptor(globalThis, "FontFace");
  const added: Array<{ descriptors: Record<string, string> }> = [];
  const fakeDocument = {
    fonts: {
      add(face: { descriptors: Record<string, string> }) {
        added.push(face);
      },
    },
  };
  class FakeFontFace {
    descriptors: Record<string, string>;

    constructor(_family: string, _source: string, descriptors: Record<string, string>) {
      this.descriptors = descriptors;
    }
  }
  try {
    Object.defineProperty(globalThis, "document", { value: fakeDocument, configurable: true });
    Object.defineProperty(globalThis, "FontFace", { value: FakeFontFace, configurable: true });
    resetFontEnumerationCacheForTests();
    const count = registerInstalledFontFaces([
      {
        family: "Source Code Pro",
        files: [
          { id: "source-code-pro-wght", weight: 200, style: "ExtraLight" },
          { id: "source-code-pro-wght", weight: 400, style: "Regular" },
          { id: "source-code-pro-wght", weight: 900, style: "Black Italic" },
        ],
      },
    ]);
    assert.equal(count, 3);
    assert.deepEqual(
      added.map((face) => face.descriptors),
      [
        { weight: "200", style: "normal" },
        { weight: "400", style: "normal" },
        { weight: "900", style: "italic" },
      ],
    );
  } finally {
    resetFontEnumerationCacheForTests();
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete (globalThis as { document?: unknown }).document;
    if (originalFontFace) Object.defineProperty(globalThis, "FontFace", originalFontFace);
    else delete (globalThis as { FontFace?: unknown }).FontFace;
  }
});
