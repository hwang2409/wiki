export type FontChoice = {
  label: string;
  family: string;
  stack: string;
};

export type InstalledFontFile = {
  id: string;
  weight?: number;
  style?: string;
};

export type InstalledFontFamily = {
  family: string;
  files: InstalledFontFile[];
};

// Backend enumerates installed OS families once and caches. The first fetch
// on cold start may take several seconds while system_profiler runs; every
// later call is instant. We cache the resolved promise in-module so multiple
// FontRoleRow instances share the single request.
let pending: Promise<InstalledFontFamily[]> | null = null;
let cached: InstalledFontFamily[] | null = null;
const registeredFamilies = new Set<string>();

function quoteFamily(family: string): string {
  return `"${family.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

function parseInstalledFonts(body: unknown): InstalledFontFamily[] {
  if (!body || typeof body !== "object") return [];
  const value = body as { fonts?: unknown; families?: unknown };
  if (Array.isArray(value.fonts)) {
    return value.fonts.flatMap((entry): InstalledFontFamily[] => {
      if (!entry || typeof entry !== "object") return [];
      const item = entry as { family?: unknown; files?: unknown };
      if (typeof item.family !== "string") return [];
      const files = Array.isArray(item.files)
        ? item.files.flatMap((file): InstalledFontFile[] => {
            if (!file || typeof file !== "object") return [];
            const candidate = file as { id?: unknown; weight?: unknown; style?: unknown };
            if (typeof candidate.id !== "string") return [];
            return [
              {
                id: candidate.id,
                ...(typeof candidate.weight === "number" ? { weight: candidate.weight } : {}),
                ...(typeof candidate.style === "string" ? { style: candidate.style } : {}),
              },
            ];
          })
        : [];
      return [{ family: item.family, files }];
    });
  }
  if (!Array.isArray(value.families)) return [];
  return value.families
    .filter((family): family is string => typeof family === "string")
    .map((family) => ({ family, files: [] }));
}

export function isEnumeratedFamily(family: string): boolean {
  return registeredFamilies.has(family);
}

export function registerInstalledFontFaces(
  fonts: InstalledFontFamily[],
  isLocallyResolvable: (family: string) => boolean = () => false,
): number {
  if (typeof document === "undefined" || !document.fonts || typeof globalThis.FontFace !== "function") {
    fonts.forEach((font) => registeredFamilies.add(font.family));
    return 0;
  }
  let registered = 0;
  for (const font of fonts) {
    registeredFamilies.add(font.family);
    let local = false;
    try {
      local = isLocallyResolvable(font.family);
    } catch {
      local = false;
    }
    if (local || font.files.length === 0) continue;
    for (const file of font.files) {
      const key = `${font.family}\u0000${file.id}`;
      if (registeredFamilies.has(key)) continue;
      const face = new globalThis.FontFace(font.family, `url("/api/fonts/file/${encodeURIComponent(file.id)}")`, {
        weight: String(file.weight ?? 400),
        style: file.style?.toLowerCase().includes("italic")
          ? "italic"
          : file.style?.toLowerCase().includes("oblique")
            ? "oblique"
            : "normal",
      });
      document.fonts.add(face);
      registeredFamilies.add(key);
      registered += 1;
    }
  }
  return registered;
}

export async function loadInstalledFontsForClassification(fonts: InstalledFontFamily[]): Promise<void> {
  if (typeof document === "undefined" || !document.fonts?.load) return;
  await Promise.all(
    fonts.map((font) => {
      const file = font.files.find((candidate) => {
        const style = candidate.style?.toLowerCase() ?? "";
        return !style.includes("italic") && !style.includes("oblique");
      }) ?? font.files[0];
      const weight = file?.weight ?? 400;
      return document.fonts.load(`${weight} 72px ${quoteFamily(font.family)}`, "mmmmmmmmwwwwwwwlliOoZ01").catch(() => []);
    }),
  );
}

export async function fetchInstalledFonts(
  options: { isLocallyResolvable?: (family: string) => boolean } = {},
): Promise<InstalledFontFamily[]> {
  if (cached !== null) return cached;
  if (pending) return pending;
  pending = (async () => {
    try {
      const response = await fetch("/api/fonts", { cache: "no-store" });
      if (!response.ok) return [];
      const fonts = parseInstalledFonts(await response.json());
      registerInstalledFontFaces(fonts, options.isLocallyResolvable);
      cached = fonts;
      return fonts;
    } catch {
      return [];
    } finally {
      pending = null;
    }
  })();
  return pending;
}

export async function fetchInstalledFamilies(
  options: { isLocallyResolvable?: (family: string) => boolean } = {},
): Promise<string[]> {
  const fonts = await fetchInstalledFonts(options);
  return fonts.map((font) => font.family);
}

export function resetFontEnumerationCacheForTests(): void {
  cached = null;
  pending = null;
  registeredFamilies.clear();
}

const MONO_TAIL =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const SANS_TAIL =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif';

export function synthesizedChoice(family: string, isMono: boolean): FontChoice {
  const tail = isMono ? MONO_TAIL : SANS_TAIL;
  return { label: family, family, stack: `${quoteFamily(family)}, ${tail}` };
}

// Canvas metrics: a proportional font renders "i" narrower than "W". A
// monospace face renders identical advance widths. Falls back to false if
// the browser cannot measure (SSR, disabled canvas).
export type MonoProbe = (family: string) => boolean;

export function makeCanvasMonoProbe(): MonoProbe {
  const cache = new Map<string, boolean>();
  return (family: string) => {
    if (cache.has(family)) return cache.get(family)!;
    try {
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      if (!ctx) return false;
      const quoted = quoteFamily(family);
      ctx.font = `72px ${quoted}, monospace`;
      const narrow = ctx.measureText("iiiiiiiiii").width;
      const wide = ctx.measureText("WWWWWWWWWW").width;
      const isMono = Math.abs(narrow - wide) < 0.5;
      cache.set(family, isMono);
      return isMono;
    } catch {
      return false;
    }
  };
}

// Merge curated pool with enumerated families. Curated entries always survive
// (they carry hand-tuned labels + stacks). Enumerated families are appended in
// enumeration order, one entry per exact family name. No dedupe beyond exact
// family identity — "JetBrains Mono" and "JetBrainsMono Nerd Font" both land.
export function mergePools({
  curated,
  installed,
  synthesize,
}: {
  curated: FontChoice[];
  installed: string[];
  synthesize: (family: string) => FontChoice;
}): FontChoice[] {
  const seenFamilies = new Set<string>();
  const merged: FontChoice[] = [];
  for (const choice of curated) {
    if (seenFamilies.has(choice.family)) continue;
    seenFamilies.add(choice.family);
    merged.push(choice);
  }
  for (const family of installed) {
    if (seenFamilies.has(family)) continue;
    seenFamilies.add(family);
    merged.push(synthesize(family));
  }
  return merged;
}

// Split enumerated families into mono vs proportional buckets. Curated pools
// stay intact per-role; enumerated families join the role whose classification
// they match. Unclassified (probe throws) falls into the proportional bucket
// since misfiling a text font as mono is more visible than the reverse.
export function classifyEnumerated(
  installed: string[],
  isMono: MonoProbe,
): { mono: string[]; prop: string[] } {
  const mono: string[] = [];
  const prop: string[] = [];
  for (const family of installed) {
    if (isMono(family)) mono.push(family);
    else prop.push(family);
  }
  return { mono, prop };
}
