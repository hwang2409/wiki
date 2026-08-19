import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import {
  INSTALLED_FONT_FACE_REGISTERED_EVENT,
  registerInstalledFontFaces,
  resetFontEnumerationCacheForTests,
} from "../src/font-enumeration";
import {
  detectFontWeights,
  handleInstalledFontFaceRegistered,
  MONO_FONTS,
} from "../src/settings";

const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalFontFace = Object.getOwnPropertyDescriptor(globalThis, "FontFace");

afterEach(() => {
  resetFontEnumerationCacheForTests();
  if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
  else delete (globalThis as { document?: unknown }).document;
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else delete (globalThis as { window?: unknown }).window;
  if (originalFontFace) Object.defineProperty(globalThis, "FontFace", originalFontFace);
  else delete (globalThis as { FontFace?: unknown }).FontFace;
});

test("JetBrains Mono NL uses the bundled family before installed fallbacks", () => {
  const bundled = MONO_FONTS.find((font) => font.label === "JetBrains Mono NL");
  const nerdMono = MONO_FONTS.find(
    (font) => font.label === "JetBrainsMonoNL Nerd Font Mono",
  );
  const nerdPropo = MONO_FONTS.find(
    (font) => font.label === "JetBrainsMonoNL Nerd Font Propo",
  );
  const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

  expect(bundled?.family).toBe("JetBrains Mono NL");
  expect(bundled?.stack).toMatch(
    /^"JetBrains Mono NL", "JetBrainsMonoNL Nerd Font Mono", "JetBrainsMonoNL Nerd Font Propo"/,
  );
  expect(nerdMono?.family).toBe("JetBrainsMonoNL Nerd Font Mono");
  expect(nerdPropo?.family).toBe("JetBrainsMonoNL Nerd Font Propo");
  const expectedFaces = [
    ["Regular", 400, "normal"],
    ["Italic", 400, "italic"],
    ["Medium", 500, "normal"],
    ["SemiBold", 600, "normal"],
    ["Bold", 700, "normal"],
    ["BoldItalic", 700, "italic"],
  ] as const;
  for (const [file, weight, style] of expectedFaces) {
    expect(styles).toContain(
      `font-family: "JetBrains Mono NL";\n  src: url("/fonts/JetBrainsMonoNL-${file}.ttf") format("truetype");\n  font-weight: ${weight};\n  font-style: ${style};\n  font-display: block;`,
    );
  }
});

test("font registration invalidates a cached family after the row selected another family", () => {
  const family = "Registered Family";
  const currentSelection = "Other Family";
  const fakeWindow = new EventTarget();
  const faces: Array<{ family: string; weight: string }> = [];
  const context = {
    font: "",
    measureText() {
      const weight = Number(context.font.match(/^\d+/)?.[0] ?? 400);
      const width = weight === 700 ? 200 : 100;
      return {
        width,
        actualBoundingBoxLeft: 0,
        actualBoundingBoxRight: width,
        actualBoundingBoxAscent: 50,
        actualBoundingBoxDescent: 10,
      };
    },
  };
  const fakeDocument = {
    createElement() {
      return { getContext: () => context };
    },
    fonts: {
      add(face: { family: string; descriptors: { weight: string } }) {
        faces.push({ family: face.family, weight: face.descriptors.weight });
      },
      *[Symbol.iterator]() {
        yield* faces;
      },
    },
  };
  class FakeFontFace {
    family: string;
    descriptors: { weight: string };

    constructor(_family: string, _source: string, descriptors: { weight: string }) {
      this.family = _family;
      this.descriptors = descriptors;
    }
  }

  Object.defineProperty(globalThis, "document", { value: fakeDocument, configurable: true });
  Object.defineProperty(globalThis, "window", { value: fakeWindow, configurable: true });
  Object.defineProperty(globalThis, "FontFace", { value: FakeFontFace, configurable: true });

  if (currentSelection === family) throw new Error("test requires a different current selection");
  expectWeights(detectFontWeights(family), [400]);
  fakeWindow.addEventListener(INSTALLED_FONT_FACE_REGISTERED_EVENT, handleInstalledFontFaceRegistered);
  registerInstalledFontFaces([{ family, files: [{ id: "registered", weight: 700 }] }]);
  expectWeights(detectFontWeights(family), [700]);
});

function expectWeights(actual: number[], expected: number[]): void {
  if (actual.length !== expected.length || actual.some((value, index) => value !== expected[index])) {
    throw new Error(`expected weights ${expected.join(", ")}, got ${actual.join(", ")}`);
  }
}
