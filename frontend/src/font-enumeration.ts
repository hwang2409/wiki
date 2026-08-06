export type FontChoice = {
  label: string;
  family: string;
  stack: string;
};

// Backend enumerates installed OS families once and caches. The first fetch
// on cold start may take several seconds while system_profiler runs; every
// later call is instant. We cache the resolved promise in-module so multiple
// FontRoleRow instances share the single request.
let pending: Promise<string[]> | null = null;
let cached: string[] | null = null;

export async function fetchInstalledFamilies(): Promise<string[]> {
  if (cached !== null) return cached;
  if (pending) return pending;
  pending = (async () => {
    try {
      const response = await fetch("/api/fonts", { cache: "no-store" });
      if (!response.ok) return [];
      const body = (await response.json()) as { families?: unknown };
      if (!Array.isArray(body.families)) return [];
      const families = body.families.filter((entry): entry is string => typeof entry === "string");
      cached = families;
      return families;
    } catch {
      return [];
    } finally {
      pending = null;
    }
  })();
  return pending;
}

export function resetFontEnumerationCacheForTests(): void {
  cached = null;
  pending = null;
}

const MONO_TAIL =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const SANS_TAIL =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif';

function quoteFamily(family: string): string {
  return `"${family.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

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
