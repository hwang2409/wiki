import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { THEMES, type ThemeId } from "./themes";
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

const BODY_SIZE_KEY = "wiki-font-size-body";
const UI_SIZE_KEY = "wiki-font-size-ui";
const MONO_SIZE_KEY = "wiki-font-size-mono";
const BODY_SIZE_DEFAULT = 16.5;
const UI_SIZE_DEFAULT = 13.5;
const MONO_SIZE_DEFAULT = 13.5;

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

export const ALL_FONTS: FontChoice[] = dedupeByLabel(MONO_FONTS, UI_FONTS, TEXT_FONTS);
// Same pool, but System-first so the agent chat role defaults to the current
// (system sans) transcript appearance instead of a mono face.
export const AGENT_FONTS: FontChoice[] = dedupeByLabel(UI_FONTS, TEXT_FONTS, MONO_FONTS);

const MONO_SAMPLE = "→ const x = 0O1lIi";
const PROP_SAMPLE = "The quick brown fox";

type FontRoleId = "ui" | "text" | "agent" | "mono";
type FontRole = {
  name: string;
  desc: string;
  curated: FontChoice[];
  // Which bucket of enumerated OS families appends to this role's pool.
  // Mono roles get canvas-classified monospace families; prop roles get the
  // rest. Curated fonts always survive regardless of classification.
  enumeratedBucket: "mono" | "prop";
  key: string;
  cssVar: string;
  weightKey: string;
  weightCssVar: string;
  sample: string;
  // When set and no weight is stored, the role inherits this role's weight
  // (mirrors the CSS var fallback chain) and the control shows an explicit
  // inherited state instead of a number that disagrees with what applies.
  inheritsWeightFrom?: FontRoleId;
};

const FONT_ROLES: Record<FontRoleId, FontRole> = {
  ui: {
    name: "Interface font",
    desc: "App chrome: sidebar, tabs, buttons, status bar, dialogs.",
    curated: ALL_FONTS,
    enumeratedBucket: "prop",
    key: "wiki-ui-font",
    cssVar: "--font-interface",
    weightKey: "wiki-ui-font-weight",
    weightCssVar: "--font-interface-weight",
    sample: PROP_SAMPLE,
  },
  text: {
    name: "Note font",
    desc: "Body text of rendered notes and the source editor.",
    curated: ALL_FONTS,
    enumeratedBucket: "prop",
    key: "wiki-text-font",
    cssVar: "--font-text",
    weightKey: "wiki-text-font-weight",
    weightCssVar: "--font-text-weight",
    sample: PROP_SAMPLE,
  },
  agent: {
    name: "Agent chat font",
    desc: "Agent replies and thinking traces in session transcripts.",
    curated: AGENT_FONTS,
    enumeratedBucket: "prop",
    key: "wiki-agent-font",
    cssVar: "--font-agent-prose",
    weightKey: "wiki-agent-font-weight",
    weightCssVar: "--font-agent-prose-weight",
    sample: PROP_SAMPLE,
    inheritsWeightFrom: "text",
  },
  mono: {
    name: "Monospace font",
    desc: "Code blocks, agent transcripts, and mono UI chrome.",
    curated: MONO_FONTS,
    enumeratedBucket: "mono",
    key: "wiki-mono-font",
    cssVar: "--font-monospace",
    weightKey: "wiki-mono-font-weight",
    weightCssVar: "--font-monospace-weight",
    sample: MONO_SAMPLE,
  },
};

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

function applySizes(body: number, ui: number, mono: number) {
  const root = document.documentElement.style;
  root.setProperty("--font-text-size", `${body}px`);
  root.setProperty("--font-ui-small", `${ui}px`);
  root.setProperty("--font-ui-smaller", `${ui - 1}px`);
  root.setProperty("--font-monospace-size", `${mono}px`);
}

function storedSize(key: string, fallback: number): number {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw >= 10 && raw <= 24 ? raw : fallback;
}

function pickChoice(fonts: FontChoice[], stored: string | null, monoFallback = false): FontChoice {
  if (stored) {
    const hit = fonts.find((font) => font.label === stored);
    if (hit) return hit;
    // Stored label from a wider prior pool (e.g. before mono/prop split) or
    // from an enumerated family that has not loaded yet. Synthesize the same
    // CSS the user had, so persistence never silently swaps the applied font.
    // isAvailable() still filters the dropdown, so a missing family here
    // renders via the tail fallback instead of the wrong first entry.
    return synthesizedChoice(stored, monoFallback);
  }
  return fonts[0];
}

function applyFontVar(cssVar: string, choice: FontChoice) {
  document.documentElement.style.setProperty(cssVar, choice.stack);
}

// Any CSS font-weight is legal (1–1000): variable fonts render arbitrary
// values, static fonts round to the nearest face. Values are no longer
// restricted to the nine canonical stops (WIKI-244).
function clampWeight(weight: number): number {
  return Math.min(1000, Math.max(1, Math.round(weight)));
}

function storedWeight(role: FontRole): number | null {
  const raw = localStorage.getItem(role.weightKey);
  if (raw === null) return null;
  const weight = Number(raw);
  return Number.isFinite(weight) && weight >= 1 && weight <= 1000 ? clampWeight(weight) : null;
}

function applyFontWeightVar(cssVar: string, weight: number | null) {
  const root = document.documentElement.style;
  if (weight === null) root.removeProperty(cssVar);
  else root.setProperty(cssVar, String(weight));
}

export function applyStoredFonts() {
  for (const role of Object.values(FONT_ROLES)) {
    applyFontVar(
      role.cssVar,
      pickChoice(role.curated, localStorage.getItem(role.key), role.enumeratedBucket === "mono"),
    );
    applyFontWeightVar(role.weightCssVar, storedWeight(role));
  }
  applySizes(
    storedSize(BODY_SIZE_KEY, BODY_SIZE_DEFAULT),
    storedSize(UI_SIZE_KEY, UI_SIZE_DEFAULT),
    storedSize(MONO_SIZE_KEY, MONO_SIZE_DEFAULT)
  );
  // Warm the backend font-enumeration cache so the settings modal is
  // ready when the user opens it. Discard errors — the picker still works
  // with curated-only pools if the endpoint is missing or slow.
  void fetchInstalledFamilies({ isLocallyResolvable: isFontInstalled }).catch(() => []);
}

function currentLabel(role: FontRole): string {
  return localStorage.getItem(role.key) ?? role.curated[0].label;
}

function setFont(role: FontRole, label: string) {
  const choice = pickChoice(role.curated, label, role.enumeratedBucket === "mono");
  localStorage.setItem(role.key, choice.label);
  applyFontVar(role.cssVar, choice);
}

function setFontWeight(role: FontRole, weight: number | null) {
  if (weight === null) localStorage.removeItem(role.weightKey);
  else localStorage.setItem(role.weightKey, String(weight));
  applyFontWeightVar(role.weightCssVar, weight);
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
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // stop the settings modal's window keydown from closing the whole modal
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
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
    <div className={`font-picker${open ? " is-open" : ""}`} ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        className="font-picker-trigger"
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

// Enumerated OS families arrive from the backend once per session. Curated
// pools always render first (with their tuned stacks + labels); enumerated
// families are appended per-role using the canvas mono-classifier so the
// mono picker stays focused and the prop pickers absorb the rest.
function useInstalledFontPools(): Record<FontRoleId, FontChoice[]> {
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
    const forProp = (family: string) => synthesizedChoice(family, false);
    const forMono = (family: string) => synthesizedChoice(family, true);
    return {
      ui: mergePools({ curated: FONT_ROLES.ui.curated, installed: prop, synthesize: forProp }),
      text: mergePools({ curated: FONT_ROLES.text.curated, installed: prop, synthesize: forProp }),
      agent: mergePools({ curated: FONT_ROLES.agent.curated, installed: prop, synthesize: forProp }),
      mono: mergePools({ curated: FONT_ROLES.mono.curated, installed: mono, synthesize: forMono }),
    };
  }, [installed]);
}

function FontRoleRow({ role, fonts }: { role: FontRole; fonts: FontChoice[] }) {
  const [label, setLabel] = useState(() => currentLabel(role));
  const [weights, setWeights] = useState<number[]>([]);
  const [fontFaceRevision, setFontFaceRevision] = useState(0);
  const inheritRole = role.inheritsWeightFrom ? FONT_ROLES[role.inheritsWeightFrom] : null;
  const inheritedWeight = () => (inheritRole ? storedWeight(inheritRole) ?? 400 : 400);
  const [weight, setWeight] = useState(() => storedWeight(role) ?? inheritedWeight());
  const [weightText, setWeightText] = useState(() => {
    const saved = storedWeight(role);
    if (saved !== null) return String(saved);
    // Inheriting roles display an explicit inherited state (empty input +
    // placeholder) so the control never disagrees with the applied CSS.
    return inheritRole ? "" : "400";
  });
  const monoFallback = role.enumeratedBucket === "mono";
  const choice = useMemo(
    () => pickChoice(fonts, label, monoFallback),
    [fonts, label, monoFallback],
  );

  useEffect(() => {
    const onFaceRegistered = (event: Event) => {
      const family = (event as CustomEvent<{ family?: unknown }>).detail?.family;
      if (family !== choice.family) return;
      AVAIL_CACHE.delete(choice.family);
      WEIGHT_CACHE.delete(choice.family);
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
      const savedWeight = storedWeight(role);
      if (savedWeight === null && inheritRole) {
        // Stay in the inherited state — never auto-write a weight for a role
        // whose CSS falls back to another role's weight.
        setWeights(nextWeights);
        setWeight(inheritedWeight());
        setWeightText("");
        return;
      }
      const nextWeight = savedWeight ?? preferredWeight(nextWeights);
      if (savedWeight === null && nextWeight !== 400) {
        setFontWeight(role, nextWeight);
      }
      setWeights(nextWeights);
      setWeight(nextWeight);
      setWeightText(String(nextWeight));
    });
    return () => {
      cancelled = true;
    };
  }, [choice, role, fontFaceRevision]);

  const applyWeight = (next: number) => {
    setFontWeight(role, next);
    setWeight(next);
  };

  return (
    <div className="settings-row">
      <div className="settings-row-info">
        <div className="settings-row-name">{role.name}</div>
        <div className="settings-row-desc">{role.desc}</div>
      </div>
      <div className="font-setting-controls">
        <FontPicker
          current={label}
          fonts={fonts}
          sample={role.sample}
          weight={weight}
          onChange={(next) => {
            setFont(role, next);
            setLabel(next);
            setWeights([]);
          }}
        />
        <input
          aria-label={`${role.name} weight (1–1000)`}
          className="font-weight-input"
          inputMode="numeric"
          max={1000}
          min={1}
          placeholder={inheritRole ? "inherit" : undefined}
          step={1}
          title={inheritRole
            ? `Font weight 1–1000; empty inherits the ${inheritRole.name.toLowerCase()} weight`
            : "Font weight, any value from 1 to 1000"}
          type="number"
          value={weightText}
          onBlur={() => {
            if (weightText.trim() === "" && inheritRole) {
              // Explicitly return to the inherited state.
              setFontWeight(role, null);
              setWeight(inheritedWeight());
              setWeightText("");
              return;
            }
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
          <div aria-label={`${role.name} detected weights`} className="font-weight-stops" role="group">
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
  onClose,
  onThemeChange,
  theme,
}: {
  onClose: () => void;
  onThemeChange: (theme: ThemeId) => void;
  theme: ThemeId;
}) {
  const [bodySize, setBodySize] = useState(() => storedSize(BODY_SIZE_KEY, BODY_SIZE_DEFAULT));
  const [uiSize, setUiSize] = useState(() => storedSize(UI_SIZE_KEY, UI_SIZE_DEFAULT));
  const [monoSize, setMonoSize] = useState(() => storedSize(MONO_SIZE_KEY, MONO_SIZE_DEFAULT));
  const fontPools = useInstalledFontPools();

  function updateSizes(body: number, ui: number, mono: number) {
    setBodySize(body);
    setUiSize(ui);
    setMonoSize(mono);
    localStorage.setItem(BODY_SIZE_KEY, String(body));
    localStorage.setItem(UI_SIZE_KEY, String(ui));
    localStorage.setItem(MONO_SIZE_KEY, String(mono));
    applySizes(body, ui, mono);
  }

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <>
      <div className="settings-backdrop" onClick={onClose} />
      <div aria-modal className="dialog settings-modal" role="dialog">
        <div className="settings-header">
          <div className="dialog-title">Settings</div>
          <button aria-label="Close settings" className="session-close" type="button" onClick={onClose}>
            <X size={14} />
          </button>
        </div>
        <div className="settings-section">
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
          <FontRoleRow fonts={fontPools.ui} role={FONT_ROLES.ui} />
          <FontRoleRow fonts={fontPools.text} role={FONT_ROLES.text} />
          <div
            className="settings-preview"
            style={{ fontFamily: "var(--font-text)", fontWeight: "var(--font-text-weight)" }}
          >
            The quick brown fox jumps over the lazy dog — 0123456789
          </div>
          <FontRoleRow fonts={fontPools.agent} role={FONT_ROLES.agent} />
          <div
            className="settings-preview"
            style={{
              fontFamily: "var(--font-agent-prose)",
              fontWeight: "var(--font-agent-prose-weight, var(--font-text-weight))",
            }}
          >
            I updated the composer and reran the suite — 13 passed, 0 failed.
          </div>
          <FontRoleRow fonts={fontPools.mono} role={FONT_ROLES.mono} />
          <div
            className="settings-preview"
            style={{ fontFamily: "var(--font-monospace)", fontWeight: "var(--font-monospace-weight)" }}
          >
            wiki agent register PHO-1234 --orch phoebe {"->"} 0O1lI| fi ff
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Note font size</div>
              <div className="settings-row-desc">Body text in notes and rendered transcripts.</div>
            </div>
            <div className="settings-slider">
              <input
                max={20}
                min={13}
                step={0.5}
                type="range"
                value={bodySize}
                onChange={(event) => updateSizes(Number(event.target.value), uiSize, monoSize)}
              />
              <span className="settings-slider-value tabular-nums">{bodySize}px</span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">UI font size</div>
              <div className="settings-row-desc">
                Chrome, chips, tool rows, sidebars (smaller variant follows at −1px).
              </div>
            </div>
            <div className="settings-slider">
              <input
                max={16}
                min={11}
                step={0.5}
                type="range"
                value={uiSize}
                onChange={(event) => updateSizes(bodySize, Number(event.target.value), monoSize)}
              />
              <span className="settings-slider-value tabular-nums">{uiSize}px</span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Monospace font size</div>
              <div className="settings-row-desc">
                Code blocks, agent transcripts, tool output, bash rows.
              </div>
            </div>
            <div className="settings-slider">
              <input
                max={20}
                min={11}
                step={0.5}
                type="range"
                value={monoSize}
                onChange={(event) => updateSizes(bodySize, uiSize, Number(event.target.value))}
              />
              <span className="settings-slider-value tabular-nums">{monoSize}px</span>
            </div>
          </div>
          <button
            className="settings-reset"
            type="button"
            onClick={() => updateSizes(BODY_SIZE_DEFAULT, UI_SIZE_DEFAULT, MONO_SIZE_DEFAULT)}
          >
            Reset sizes to default
          </button>
        </div>
      </div>
    </>
  );
}
