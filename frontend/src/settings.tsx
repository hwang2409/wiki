import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { THEMES, type ThemeId } from "./themes";
import { applyLowercase, getStoredLowercase } from "./lowercase-mode";
import { BbDialog, DialogRail } from "./dialogs";
import type { FocusReturnRef } from "./modal-a11y";
import { Button } from "./primitives";
import {
  classifyEnumerated,
  fetchInstalledFamilies,
  fetchInstalledFonts,
  INSTALLED_FONT_FACE_REGISTERED_EVENT,
  isEnumeratedFamily,
  loadInstalledFontsForClassification,
  makeCanvasMonoProbe,
  mergePools,
  type InstalledFontFamily,
  synthesizedChoice,
  type FontChoice,
} from "./font-enumeration";

export type { FontChoice };

const MONO_TAIL =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const SANS_TAIL =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif';
const SERIF_TAIL =
  'Georgia, Charter, "Iowan Old Style", "Times New Roman", serif';

export const MONO_FONTS: FontChoice[] = [
  { label: "JetBrains Mono", family: "JetBrains Mono", stack: `"JetBrains Mono", ${MONO_TAIL}` },
  { label: "Geist Mono", family: "Geist Mono", stack: `"Geist Mono", ${MONO_TAIL}` },
  { label: "Fira Code", family: "Fira Code", stack: `"Fira Code", ${MONO_TAIL}` },
  { label: "IBM Plex Mono", family: "IBM Plex Mono", stack: `"IBM Plex Mono", ${MONO_TAIL}` },
  { label: "Source Code Pro", family: "Source Code Pro", stack: `"Source Code Pro", ${MONO_TAIL}` },
  { label: "Roboto Mono", family: "Roboto Mono", stack: `"Roboto Mono", ${MONO_TAIL}` },
  { label: "Inconsolata", family: "Inconsolata", stack: `"Inconsolata", ${MONO_TAIL}` },
  { label: "Space Mono", family: "Space Mono", stack: `"Space Mono", ${MONO_TAIL}` },
  { label: "Victor Mono", family: "Victor Mono", stack: `"Victor Mono", ${MONO_TAIL}` },
  { label: "Red Hat Mono", family: "Red Hat Mono", stack: `"Red Hat Mono", ${MONO_TAIL}` },
  { label: "Martian Mono", family: "Martian Mono", stack: `"Martian Mono", ${MONO_TAIL}` },
  { label: "Monaco", family: "Monaco", stack: `Monaco, ${MONO_TAIL}` },
  { label: "Menlo", family: "Menlo", stack: `Menlo, ${MONO_TAIL}` },
  { label: "Consolas", family: "Consolas", stack: `Consolas, ${MONO_TAIL}` },
  {
    label: "Consolas for Powerline",
    family: "Consolas for Powerline",
    stack: `"Consolas for Powerline", ${MONO_TAIL}`,
  },
  {
    label: "Moxy Static",
    family: "Moxy Static",
    stack: `"Moxy Static", ${MONO_TAIL}`,
  },
  { label: "Courier New", family: "Courier New", stack: `"Courier New", ${MONO_TAIL}` },
  { label: "Andale Mono", family: "Andale Mono", stack: `"Andale Mono", ${MONO_TAIL}` },
  { label: "PT Mono", family: "PT Mono", stack: `"PT Mono", ${MONO_TAIL}` },
  { label: "Cascadia Code", family: "Cascadia Code", stack: `"Cascadia Code", ${MONO_TAIL}` },
  { label: "Cascadia Mono", family: "Cascadia Mono", stack: `"Cascadia Mono", ${MONO_TAIL}` },
  { label: "Hack", family: "Hack", stack: `Hack, ${MONO_TAIL}` },
  { label: "Iosevka", family: "Iosevka", stack: `Iosevka, ${MONO_TAIL}` },
  { label: "Anonymous Pro", family: "Anonymous Pro", stack: `"Anonymous Pro", ${MONO_TAIL}` },
  {
    label: "System (SF Mono)",
    family: "ui-monospace",
    stack: `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Roboto Mono", monospace`,
  },
];

export const UI_FONTS: FontChoice[] = [
  { label: "Inter Variable", family: "Inter Variable", stack: `"Inter Variable", ${SANS_TAIL}` },
  {
    label: "System",
    family: "-apple-system",
    stack: SANS_TAIL,
  },
  { label: "Inter", family: "Inter", stack: `"Inter", ${SANS_TAIL}` },
  { label: "Helvetica Neue", family: "Helvetica Neue", stack: `"Helvetica Neue", ${SANS_TAIL}` },
  { label: "Helvetica", family: "Helvetica", stack: `Helvetica, ${SANS_TAIL}` },
  { label: "Arial", family: "Arial", stack: `Arial, ${SANS_TAIL}` },
  { label: "Avenir", family: "Avenir", stack: `Avenir, ${SANS_TAIL}` },
  { label: "Avenir Next", family: "Avenir Next", stack: `"Avenir Next", ${SANS_TAIL}` },
  { label: "Optima", family: "Optima", stack: `Optima, ${SANS_TAIL}` },
  { label: "Lucida Grande", family: "Lucida Grande", stack: `"Lucida Grande", ${SANS_TAIL}` },
  { label: "Verdana", family: "Verdana", stack: `Verdana, ${SANS_TAIL}` },
  { label: "Tahoma", family: "Tahoma", stack: `Tahoma, ${SANS_TAIL}` },
  { label: "Segoe UI", family: "Segoe UI", stack: `"Segoe UI", ${SANS_TAIL}` },
  { label: "Roboto", family: "Roboto", stack: `Roboto, ${SANS_TAIL}` },
];

export const TEXT_FONTS: FontChoice[] = [
  {
    label: "System",
    family: "-apple-system",
    stack: SANS_TAIL,
  },
  { label: "Inter", family: "Inter", stack: `"Inter", ${SANS_TAIL}` },
  { label: "Source Serif", family: "Source Serif 4", stack: `"Source Serif 4", ${SERIF_TAIL}` },
  { label: "Georgia", family: "Georgia", stack: `Georgia, ${SERIF_TAIL}` },
  { label: "Charter", family: "Charter", stack: `Charter, ${SERIF_TAIL}` },
  { label: "Iowan Old Style", family: "Iowan Old Style", stack: `"Iowan Old Style", ${SERIF_TAIL}` },
  { label: "Palatino", family: "Palatino", stack: `Palatino, ${SERIF_TAIL}` },
  { label: "Baskerville", family: "Baskerville", stack: `Baskerville, ${SERIF_TAIL}` },
  { label: "Times New Roman", family: "Times New Roman", stack: `"Times New Roman", ${SERIF_TAIL}` },
  { label: "Times", family: "Times", stack: `Times, ${SERIF_TAIL}` },
  { label: "Helvetica Neue", family: "Helvetica Neue", stack: `"Helvetica Neue", ${SANS_TAIL}` },
  { label: "Optima", family: "Optima", stack: `Optima, ${SANS_TAIL}` },
];

function dedupeByLabel(...pools: FontChoice[][]): FontChoice[] {
  const seen = new Set<string>();
  const merged: FontChoice[] = [];
  for (const pool of pools) {
    for (const choice of pool) {
      if (seen.has(choice.label)) continue;
      seen.add(choice.label);
      merged.push(choice);
    }
  }
  return merged;
}

// One unified pool: proportional families first (System, then curated prop),
// then curated mono, so the default picker lead matches the app's historical
// prose feel while every family (prop or mono) sits in the same list.
export const ALL_FONTS: FontChoice[] = dedupeByLabel(UI_FONTS, TEXT_FONTS, MONO_FONTS);
export const AGENT_FONTS: FontChoice[] = ALL_FONTS;

type SettingsSection = "appearance" | "typography";

function isSettingsSection(value: string): value is SettingsSection {
  return value === "appearance" || value === "typography";
}

// Single storage keys — one font, one weight, and one base size. The base size
// derives prose, control, and chrome tiers in styles.css (WIKI-318). Legacy
// per-role keys are read once for migration then deleted.
const FONT_KEY = "wiki-font";
// Resolved CSS stack for cross-document rendering (dashboard). Persisted so
// the dashboard doesn't need to re-import the FontChoice pool to map a label
// like "System" back to "-apple-system, ..." or synthesize an installed
// family's fallback tail.
const STACK_KEY = "wiki-font-stack";
const WEIGHT_KEY = "wiki-font-weight";
const SIZE_KEY = "wiki-font-size";
const SIZE_DEFAULT = 15;
const SIZE_MIN = 11;
const SIZE_MAX = 22;
const DEFAULT_CHROME_FONT = "Inter Variable";
const DEFAULT_MONO_FONT = "Fira Code";

const LEGACY_FONT_KEYS = [
  "wiki-ui-font",
  "wiki-text-font",
  "wiki-agent-font",
  "wiki-mono-font",
];
const LEGACY_WEIGHT_KEYS = [
  "wiki-ui-font-weight",
  "wiki-text-font-weight",
  "wiki-agent-font-weight",
  "wiki-mono-font-weight",
];
const LEGACY_SIZE_KEYS = [
  "wiki-font-size-body",
  "wiki-font-size-ui",
  "wiki-font-size-mono",
];

// Family and weight roles remain aliases of the single user choice. Size is
// written only to the base token; styles.css derives the semantic tiers.
const FAMILY_VARS = [
  "--font-single",
  "--font-interface",
  "--font-text",
  "--font-agent-prose",
  "--font-monospace",
  "--font-chrome",
];
const WEIGHT_VARS = [
  "--font-single-weight",
  "--font-interface-weight",
  "--font-text-weight",
  "--font-agent-prose-weight",
  "--font-monospace-weight",
];
const SIZE_VARS = [
  "--font-single-size",
  "--font-text-size",
  "--font-ui-small",
  "--font-ui-smaller",
  "--font-monospace-size",
];
const CHROME_FAMILY_VARS = FAMILY_VARS.filter((cssVar) => cssVar !== "--font-monospace");
const MONO_FAMILY_VARS = ["--font-monospace"];

const MONO_SAMPLE = "→ const x = 0O1lIi";
const PROP_SAMPLE = "The quick brown fox";

function firstStored(keys: string[]): string | null {
  for (const key of keys) {
    const value = localStorage.getItem(key);
    if (value !== null && value !== "") return value;
  }
  return null;
}

function migrateLegacyOnce(): void {
  if (localStorage.getItem(FONT_KEY) === null) {
    const legacyFont = firstStored(LEGACY_FONT_KEYS);
    if (legacyFont !== null) localStorage.setItem(FONT_KEY, legacyFont);
  }
  if (localStorage.getItem(WEIGHT_KEY) === null) {
    const legacyWeight = firstStored(LEGACY_WEIGHT_KEYS);
    if (legacyWeight !== null) localStorage.setItem(WEIGHT_KEY, legacyWeight);
  }
  if (localStorage.getItem(SIZE_KEY) === null) {
    const legacySize = firstStored(LEGACY_SIZE_KEYS);
    if (legacySize !== null) localStorage.setItem(SIZE_KEY, legacySize);
  }
  // Only remove legacy keys that actually exist — a bare removeItem fires the
  // ui-state write-back tombstone (WIKI-256 mirror) even for missing keys,
  // which would seed the mirror with the legacy names we're trying to erase.
  for (const key of [...LEGACY_FONT_KEYS, ...LEGACY_WEIGHT_KEYS, ...LEGACY_SIZE_KEYS]) {
    if (localStorage.getItem(key) !== null) localStorage.removeItem(key);
  }
}

async function loadFontFaces(choice: FontChoice): Promise<void> {
  if (!document.fonts) return;
  await Promise.all(
    WEIGHT_STOPS.map((weight) =>
      document.fonts.load(`${weight} 72px ${quotedFamily(choice.family)}`, AVAIL_SAMPLE).catch(() => [])
    )
  );
}

// document.fonts.check() is optimistic — returns true for anything, so it can't
// tell an installed system font from a missing one. Fall back to canvas metrics:
// if "family" renders identically under monospace and serif fallbacks, the font
// is actually installed; otherwise the fallbacks kicked in.
const AVAIL_SAMPLE = "mmmmmmmmwwwwwwwlliOoZ01";
const AVAIL_CACHE = new Map<string, boolean>();
const WEIGHT_CACHE = new Map<string, number[]>();
const WEIGHT_STOPS = [100, 200, 300, 400, 500, 600, 700, 800, 900];
const WEIGHT_LABELS: Record<number, string> = {
  100: "Thin",
  200: "Extra Light",
  300: "Light",
  400: "Regular",
  500: "Medium",
  600: "Semibold",
  700: "Bold",
  800: "Extra Bold",
  900: "Black",
};

export function invalidateFontCachesForFamily(family: string): void {
  AVAIL_CACHE.delete(family);
  WEIGHT_CACHE.delete(family);
}

export function handleInstalledFontFaceRegistered(event: Event): string | undefined {
  const family = (event as CustomEvent<{ family?: unknown }>).detail?.family;
  if (typeof family !== "string") return undefined;
  invalidateFontCachesForFamily(family);
  return family;
}

function isFontInstalled(family: string): boolean {
  const cached = AVAIL_CACHE.get(family);
  if (cached !== undefined) return cached;
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return true;
    const measure = (spec: string) => {
      ctx.font = `72px ${spec}`;
      return ctx.measureText(AVAIL_SAMPLE).width;
    };
    const quoted = `"${family}"`;
    const installed =
      measure(`${quoted}, monospace`) === measure(`${quoted}, serif`) &&
      measure(`${quoted}, monospace`) === measure(`${quoted}, sans-serif`);
    AVAIL_CACHE.set(family, installed);
    return installed;
  } catch {
    return true;
  }
}

const ALWAYS_AVAILABLE_FAMILIES = new Set([
  "ui-monospace",
  "-apple-system",
  "Consolas for Powerline",
  "Moxy Static",
]);

function isAvailable(choice: FontChoice): boolean {
  if (ALWAYS_AVAILABLE_FAMILIES.has(choice.family)) return true;
  if (isEnumeratedFamily(choice.family)) return true;
  return isFontInstalled(choice.family);
}

function quotedFamily(family: string): string {
  return `"${family.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

function measureWeightSignature(ctx: CanvasRenderingContext2D, family: string, weight: number): string {
  ctx.font = `${weight} 72px ${quotedFamily(family)}`;
  const metrics = ctx.measureText(AVAIL_SAMPLE);
  // Advance width catches most faces. The ink bounds matter for fixed-width
  // faces, whose advance stays constant while their real weights differ.
  return [
    metrics.width,
    metrics.actualBoundingBoxLeft,
    metrics.actualBoundingBoxRight,
    metrics.actualBoundingBoxAscent,
    metrics.actualBoundingBoxDescent,
  ]
    .map((value) => (Number.isFinite(value) ? value.toFixed(3) : ""))
    .join("|");
}

function preferredWeight(weights: number[]): number {
  return weights.reduce((best, weight) =>
    Math.abs(weight - 400) < Math.abs(best - 400) ? weight : best
  );
}

function declaredWeightStops(family: string): Set<number> {
  const familyName = family.replaceAll('"', "");
  const declared = new Set<number>();
  for (const face of document.fonts) {
    if (face.family.replaceAll('"', "") !== familyName) continue;
    const values = [...face.weight.matchAll(/\d+/g)].map((match) => Number(match[0]));
    if (values.length === 1 && WEIGHT_STOPS.includes(values[0])) declared.add(values[0]);
    if (values.length >= 2) {
      for (const stop of WEIGHT_STOPS) {
        if (stop >= values[0] && stop <= values[1]) declared.add(stop);
      }
    }
  }
  return declared;
}

function groupWeight(weights: number[], declared: Set<number>): number {
  const realWeights = weights.filter((weight) => declared.has(weight));
  return preferredWeight(realWeights.length > 0 ? realWeights : weights);
}

// Canvas uses the browser's actual font selection path, unlike
// document.fonts.check(), which says any requested family/weight is present.
// Identical metrics map to one face, so browser fallback and repeated aliases
// do not become fake choices. Keeping the stop nearest Regular makes a 400
// face report as Regular rather than the first equivalent probe (100).
export function detectFontWeights(family: string): number[] {
  const cached = WEIGHT_CACHE.get(family);
  if (cached) return cached;
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return [400];
    const bySignature = new Map<string, number[]>();
    for (const weight of WEIGHT_STOPS) {
      const signature = measureWeightSignature(ctx, family, weight);
      const weights = bySignature.get(signature) ?? [];
      weights.push(weight);
      bySignature.set(signature, weights);
    }
    const declared = declaredWeightStops(family);
    const detected = [...bySignature.values()]
      // When CSS has declared faces (bundled and dynamically loaded fonts),
      // reject a canvas-only signature: it is browser synthesis, not a face.
      .filter((weights) => weights.some((weight) => declared.has(weight)))
      .map((weights) => groupWeight(weights, declared))
      .sort((left, right) => left - right);
    const result = detected.length > 0 ? detected : [400];
    WEIGHT_CACHE.set(family, result);
    return result;
  } catch {
    return [400];
  }
}

function weightLabel(weight: number): string {
  return WEIGHT_LABELS[weight] ?? String(weight);
}

function applyFamilyEverywhere(choice: FontChoice) {
  const root = document.documentElement.style;
  for (const cssVar of FAMILY_VARS) root.setProperty(cssVar, choice.stack);
  // Mirror the resolved stack for the dashboard document. Only write when
  // the value changed — the ui-state mirror is idempotent but noisy writes
  // still push bytes.
  if (localStorage.getItem(STACK_KEY) !== choice.stack) {
    localStorage.setItem(STACK_KEY, choice.stack);
  }
}

function applyNewInstallFamilies() {
  const chrome = pickChoice(ALL_FONTS, DEFAULT_CHROME_FONT);
  const mono = pickChoice(ALL_FONTS, DEFAULT_MONO_FONT);
  const root = document.documentElement.style;
  for (const cssVar of CHROME_FAMILY_VARS) root.setProperty(cssVar, chrome.stack);
  for (const cssVar of MONO_FAMILY_VARS) root.setProperty(cssVar, mono.stack);
  if (localStorage.getItem(STACK_KEY) !== chrome.stack) {
    localStorage.setItem(STACK_KEY, chrome.stack);
  }
}

function applyWeightEverywhere(weight: number | null) {
  const root = document.documentElement.style;
  for (const cssVar of WEIGHT_VARS) {
    if (weight === null) root.removeProperty(cssVar);
    else root.setProperty(cssVar, String(weight));
  }
}

function applySizeEverywhere(size: number) {
  const root = document.documentElement.style;
  const value = `${size}px`;
  root.setProperty("--font-single-size", value);
}

function storedSize(): number {
  const raw = Number(localStorage.getItem(SIZE_KEY));
  return Number.isFinite(raw) && raw >= SIZE_MIN && raw <= SIZE_MAX ? raw : SIZE_DEFAULT;
}

function pickChoice(fonts: FontChoice[], stored: string | null): FontChoice {
  if (stored) {
    const hit = fonts.find((font) => font.label === stored);
    if (hit) return hit;
    // Stored label from a wider prior pool (e.g. an enumerated OS family that
    // hasn't loaded yet, or a legacy mono-only pool). Synthesize the same CSS
    // the user had so persistence never silently swaps the applied font.
    return synthesizedChoice(stored, false);
  }
  return fonts[0];
}

// Any CSS font-weight is legal (1–1000): variable fonts render arbitrary
// values, static fonts round to the nearest face.
function clampWeight(weight: number): number {
  return Math.min(1000, Math.max(1, Math.round(weight)));
}

function storedWeight(): number | null {
  const raw = localStorage.getItem(WEIGHT_KEY);
  if (raw === null) return null;
  const weight = Number(raw);
  return Number.isFinite(weight) && weight >= 1 && weight <= 1000 ? clampWeight(weight) : null;
}

export function applyStoredFonts() {
  migrateLegacyOnce();
  const stored = localStorage.getItem(FONT_KEY);
  if (stored === null) applyNewInstallFamilies();
  else applyFamilyEverywhere(pickChoice(ALL_FONTS, stored));
  applyWeightEverywhere(storedWeight());
  applySizeEverywhere(storedSize());
  // Warm the backend font-enumeration cache so the settings modal is
  // ready when the user opens it. Discard errors — the picker still works
  // with curated-only pools if the endpoint is missing or slow.
  void fetchInstalledFamilies({ isLocallyResolvable: isFontInstalled }).catch(() => []);
}

function currentLabel(): string {
  return localStorage.getItem(FONT_KEY) ?? DEFAULT_CHROME_FONT;
}

function persistFontLabel(label: string, fonts: FontChoice[]) {
  const choice = pickChoice(fonts, label);
  localStorage.setItem(FONT_KEY, choice.label);
  applyFamilyEverywhere(choice);
}

function setFontWeight(weight: number | null) {
  if (weight === null) localStorage.removeItem(WEIGHT_KEY);
  else localStorage.setItem(WEIGHT_KEY, String(weight));
  applyWeightEverywhere(weight);
}

function setFontSize(size: number) {
  localStorage.setItem(SIZE_KEY, String(size));
  applySizeEverywhere(size);
}

function FontPicker({
  fonts,
  current,
  sample,
  weight,
  onChange,
}: {
  fonts: FontChoice[];
  current: string;
  sample: string;
  weight: number;
  onChange: (label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [availTick, setAvailTick] = useState(0);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const filterRef = useRef<HTMLInputElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const wasOpenRef = useRef(false);

  const currentChoice = useMemo(() => pickChoice(fonts, current), [fonts, current]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    setAvailTick((t) => t + 1);
  }, [open, fonts]);

  useEffect(() => {
    if (!open) return;
    void Promise.all(fonts.map(loadFontFaces));
  }, [open, fonts]);

  useEffect(() => {
    if (!open) return;
    // Focus the filter on open so power users can type-narrow immediately.
    filterRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
    };
  }, [open]);

  useEffect(() => {
    if (wasOpenRef.current && !open) triggerRef.current?.focus();
    wasOpenRef.current = open;
  }, [open]);

  const available = useMemo(
    () => fonts.filter((font) => font.label === current || isAvailable(font)),
    // availTick invalidates the memo after loads land
    [fonts, current, availTick]
  );
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return available;
    return available.filter((font) => font.label.toLowerCase().includes(needle));
  }, [available, query]);

  return (
    <div
      className={`font-picker${open ? " is-open" : ""}`}
      ref={rootRef}
      onKeyDown={(event) => {
        if (!open || event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }}
    >
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        className="font-picker-trigger"
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
      >
        <span
          className="font-picker-trigger-label"
          style={{ fontFamily: currentChoice.stack, fontWeight: weight }}
        >
          {currentChoice.label}
        </span>
        <ChevronDown aria-hidden className="font-picker-trigger-chevron" size={14} />
      </button>
      {open ? (
        <div className="font-picker-menu" role="listbox">
          <input
            aria-label="Filter fonts"
            className="font-picker-filter"
            placeholder="Filter fonts"
            ref={filterRef}
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && visible.length > 0) {
                event.preventDefault();
                onChange(visible[0].label);
                setOpen(false);
              }
            }}
          />
          {visible.length === 0 ? (
            <div className="font-picker-empty">No fonts match “{query}”.</div>
          ) : null}
          {visible.map((font) => {
            const active = font.label === current;
            return (
              <button
                aria-selected={active}
                className={`font-picker-option${active ? " is-active" : ""}`}
                key={font.label}
                role="option"
                type="button"
                onClick={() => {
                  onChange(font.label);
                  setOpen(false);
                }}
              >
                <span
                  className="font-picker-option-label"
                  style={{ fontFamily: font.stack, fontWeight: weight }}
                >
                  {font.label}
                </span>
                <span
                  aria-hidden
                  className="font-picker-option-sample"
                  style={{ fontFamily: font.stack, fontWeight: weight }}
                >
                  {sample}
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

// Enumerated OS families arrive from the backend once per session. The
// unified single-font pool absorbs BOTH mono and proportional buckets, each
// synthesized with the right fallback tail so the browser picks the right
// generic when the installed family is missing.
function useInstalledFontPool(): FontChoice[] {
  const [installed, setInstalled] = useState<InstalledFontFamily[]>([]);
  useEffect(() => {
    let cancelled = false;
    void fetchInstalledFonts({ isLocallyResolvable: isFontInstalled }).then(async (fonts) => {
      await loadInstalledFontsForClassification(fonts);
      if (cancelled) return;
      setInstalled(fonts);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return useMemo(() => {
    const probe = makeCanvasMonoProbe();
    const { mono, prop } = classifyEnumerated(installed.map((font) => font.family), probe);
    const withProp = mergePools({
      curated: ALL_FONTS,
      installed: prop,
      synthesize: (family) => synthesizedChoice(family, false),
    });
    return mergePools({
      curated: withProp,
      installed: mono,
      synthesize: (family) => synthesizedChoice(family, true),
    });
  }, [installed]);
}

function FontRow({
  fonts,
  label,
  weight,
  onFontChange,
  onWeightChange,
}: {
  fonts: FontChoice[];
  label: string;
  weight: number;
  onFontChange: (label: string) => void;
  onWeightChange: (weight: number) => void;
}) {
  const [weights, setWeights] = useState<number[]>([]);
  const [fontFaceRevision, setFontFaceRevision] = useState(0);
  const [weightText, setWeightText] = useState(() => String(storedWeight() ?? 400));
  const choice = useMemo(() => pickChoice(fonts, label), [fonts, label]);

  useEffect(() => {
    const onFaceRegistered = (event: Event) => {
      const family = handleInstalledFontFaceRegistered(event);
      if (family !== choice.family) return;
      setFontFaceRevision((revision) => revision + 1);
    };
    window.addEventListener(INSTALLED_FONT_FACE_REGISTERED_EVENT, onFaceRegistered);
    return () => window.removeEventListener(INSTALLED_FONT_FACE_REGISTERED_EVENT, onFaceRegistered);
  }, [choice.family]);

  useEffect(() => {
    let cancelled = false;
    void loadFontFaces(choice).then(() => {
      if (cancelled) return;
      const nextWeights = detectFontWeights(choice.family);
      // A saved weight is respected as-is, even off the detected stops —
      // arbitrary values are the point (variable fonts). Only the unset case
      // adopts the face's preferred default.
      const savedWeight = storedWeight();
      const nextWeight = savedWeight ?? preferredWeight(nextWeights);
      if (savedWeight === null && nextWeight !== 400) {
        setFontWeight(nextWeight);
      }
      setWeights(nextWeights);
      onWeightChange(nextWeight);
      setWeightText(String(nextWeight));
    });
    return () => {
      cancelled = true;
    };
  }, [choice, fontFaceRevision, onWeightChange]);

  const applyWeight = (next: number) => {
    setFontWeight(next);
    onWeightChange(next);
  };

  return (
    <div className="settings-row">
      <div className="settings-row-info">
        <div className="settings-row-name">Font</div>
        <div className="settings-row-desc">
          One family for every surface — prose, chat, code, diffs, chrome, dashboard.
        </div>
      </div>
      <div className="font-setting-controls">
        <FontPicker
          current={label}
          fonts={fonts}
          sample={PROP_SAMPLE}
          weight={weight}
          onChange={(next) => {
            onFontChange(next);
            setWeights([]);
          }}
        />
        <input
          aria-label="Font weight (1–1000)"
          className="font-weight-input"
          inputMode="numeric"
          max={1000}
          min={1}
          step={1}
          title="Font weight, any value from 1 to 1000"
          type="number"
          value={weightText}
          onBlur={() => {
            const parsed = Number(weightText);
            const next = Number.isFinite(parsed) && weightText.trim() !== "" ? clampWeight(parsed) : weight;
            applyWeight(next);
            setWeightText(String(next));
          }}
          onChange={(event) => {
            const raw = event.target.value;
            setWeightText(raw);
            const parsed = Number(raw);
            if (Number.isFinite(parsed) && parsed >= 1 && parsed <= 1000) {
              applyWeight(clampWeight(parsed));
            }
          }}
        />
        {weights.length > 1 ? (
          <div aria-label="Detected font weights" className="font-weight-stops" role="group">
            {weights.map((option) => (
              <button
                key={option}
                className={`font-weight-stop${option === weight ? " is-active" : ""}`}
                title={weightLabel(option)}
                type="button"
                onClick={() => {
                  applyWeight(option);
                  setWeightText(String(option));
                }}
              >
                {option}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function SettingsModal({
  fallbackRef,
  onClose,
  onThemeChange,
  theme,
}: {
  fallbackRef?: FocusReturnRef;
  onClose: () => void;
  onThemeChange: (theme: ThemeId) => void;
  theme: ThemeId;
}) {
  const [size, setSize] = useState(() => storedSize());
  const [lowercase, setLowercase] = useState(() => getStoredLowercase());
  const [fontLabel, setFontLabel] = useState(() => currentLabel());
  const [fontWeight, setFontWeightState] = useState(() => storedWeight() ?? 400);
  const [section, setSection] = useState<SettingsSection>("appearance");
  const fontPool = useInstalledFontPool();

  function updateSize(next: number) {
    setSize(next);
    setFontSize(next);
  }

  return (
    <BbDialog
      title="Settings"
      description="Applied immediately. Close when you are done."
      size="lg"
      onClose={onClose}
      fallbackRef={fallbackRef}
      rail={
        <DialogRail
          active={section}
          items={[
            { id: "appearance", label: "Appearance" },
            { id: "typography", label: "Typography" },
          ]}
          onSelect={(id) => {
            if (isSettingsSection(id)) setSection(id);
          }}
        />
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="default" onClick={onClose}>
            Done
          </Button>
        </>
      }
    >
      {section === "appearance" ? (
        <div
          id="bb-rail-panel-appearance"
          role="tabpanel"
          aria-labelledby="bb-rail-appearance"
          className="bb-dialog__group"
        >
          <div className="settings-row settings-row-stack">
            <div className="settings-row-info">
              <div className="settings-row-name">Theme</div>
              <div className="settings-row-desc">
                UI chrome, diff accents, and syntax highlighting follow the selected palette.
              </div>
            </div>
            <div aria-label="Theme" className="theme-grid" role="radiogroup">
              {THEMES.map((option) => (
                <button
                  key={option.id}
                  aria-checked={theme === option.id}
                  className={`theme-choice${theme === option.id ? " is-active" : ""}`}
                  role="radio"
                  type="button"
                  onClick={() => onThemeChange(option.id)}
                >
                  <span aria-hidden className="theme-choice-swatches">
                    {option.preview.map((color, index) => (
                      <span
                        key={`${option.id}-${index}`}
                        className="theme-choice-swatch"
                        style={{ backgroundColor: color }}
                      />
                    ))}
                  </span>
                  <span className="theme-choice-name">{option.label}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Lowercase mode</div>
              <div className="settings-row-desc">
                Render every label, note, and transcript in lowercase. Code, diffs, and terminal output keep their original case.
              </div>
            </div>
            <button
              aria-checked={lowercase}
              aria-label="Lowercase mode"
              className={`settings-toggle${lowercase ? " is-on" : ""}`}
              role="switch"
              type="button"
              onClick={() => {
                const next = !lowercase;
                setLowercase(next);
                applyLowercase(next);
              }}
            >
              <span aria-hidden className="settings-toggle-thumb" />
            </button>
          </div>
        </div>
      ) : (
        <div
          id="bb-rail-panel-typography"
          role="tabpanel"
          aria-labelledby="bb-rail-typography"
          className="bb-dialog__group"
        >
          <FontRow
            fonts={fontPool}
            label={fontLabel}
            weight={fontWeight}
            onFontChange={(next) => {
              setFontLabel(next);
              persistFontLabel(next, fontPool);
            }}
            onWeightChange={setFontWeightState}
          />
          <div
            className="settings-preview"
            style={{ fontFamily: "var(--font-single)", fontWeight: "var(--font-single-weight)", fontSize: "var(--font-single-size)" }}
          >
            {PROP_SAMPLE} — 0123456789
          </div>
          <div
            className="settings-preview"
            style={{ fontFamily: "var(--font-single)", fontWeight: "var(--font-single-weight)", fontSize: "var(--font-single-size)" }}
          >
            {MONO_SAMPLE}  wiki agent register --orch phoebe
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Size</div>
              <div className="settings-row-desc">
                One base size derives 15px prose, 13px controls, and 10px chrome.
                All tiers scale together.
              </div>
            </div>
            <div className="settings-slider">
              <input
                max={SIZE_MAX}
                min={SIZE_MIN}
                step={0.5}
                type="range"
                value={size}
                onChange={(event) => updateSize(Number(event.target.value))}
              />
              <span className="settings-slider-value tabular-nums">{size}px</span>
            </div>
          </div>
          <button
            className="settings-reset"
            type="button"
            onClick={() => updateSize(SIZE_DEFAULT)}
          >
            Reset size to default
          </button>
        </div>
      )}
    </BbDialog>
  );
}
