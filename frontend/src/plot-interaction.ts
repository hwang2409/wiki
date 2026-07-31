// Interaction logic for kind:plot artifacts (WIKI-194).
//
// Payloads are arbitrary Vega-Lite specs. Interactivity is injected only into
// single-view specs (a top-level `mark` + `encoding`); composite or malformed
// specs render exactly as before (static fallback). Pan/zoom binds only to
// continuous positional channels that are directly projectable — quantitative
// or temporal, not binned, not aggregated, not timeUnit-transformed (the
// compiled signal keys off the derived field name in those cases, so the
// React-side mapping would silently miss the extent).

export type VegaLiteSpec = Record<string, unknown>;
export type ZoomChannel = "x" | "y";
export type PlotDomains = Partial<Record<ZoomChannel, [number, number]>>;

export type PlotInteractivity =
  | { mode: "static" }
  | { mode: "tooltip" }
  | { mode: "full"; channels: ZoomChannel[]; fields: Partial<Record<ZoomChannel, string>> };

export const ZOOM_PARAM = "wiki_zoom";
export const BRUSH_PARAM = "wiki_brush";

const COMPOSITE_KEYS = ["layer", "facet", "concat", "hconcat", "vconcat", "repeat", "spec"];
const RESERVED_PARAM_NAMES = new Set([ZOOM_PARAM, BRUSH_PARAM]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function continuousField(encoding: Record<string, unknown>, channel: ZoomChannel): string | null {
  const def = asRecord(encoding[channel]);
  if (!def) return null;
  if (def.type !== "quantitative" && def.type !== "temporal") return null;
  if (def.bin) return null;
  // Aggregate encodings can't be interval-projected in Vega-Lite; timeUnit
  // encodings key the selection off a compiled name (e.g. yearmonth_ts) that
  // isn't the source field, so shift-brush would store no domain.
  if (def.aggregate) return null;
  if (def.timeUnit) return null;
  return typeof def.field === "string" && def.field.length > 0 ? def.field : null;
}

function paramNameCollision(spec: Record<string, unknown>): boolean {
  const params = Array.isArray(spec.params) ? spec.params : [];
  return params.some((param) => {
    const record = asRecord(param);
    return typeof record?.name === "string" && RESERVED_PARAM_NAMES.has(record.name);
  });
}

export function plotInteractivity(spec: unknown): PlotInteractivity {
  const record = asRecord(spec);
  if (!record || !("mark" in record)) return { mode: "static" };
  if (COMPOSITE_KEYS.some((key) => key in record)) return { mode: "static" };
  const encoding = asRecord(record.encoding);
  if (!encoding) return { mode: "static" };
  const fields: Partial<Record<ZoomChannel, string>> = {};
  const channels: ZoomChannel[] = [];
  for (const channel of ["x", "y"] as const) {
    const field = continuousField(encoding, channel);
    if (field) {
      channels.push(channel);
      fields[channel] = field;
    }
  }
  if (channels.length === 0) return { mode: "tooltip" };
  // A spec that already reserves wiki_zoom or wiki_brush would trip Vega's
  // duplicate-signal check on inject. Degrade to tooltip so the plot still
  // renders — WIKI-194 must not regress previously-working payloads.
  if (paramNameCollision(record)) return { mode: "tooltip" };
  return { mode: "full", channels, fields };
}

// Event streams gated on shift so plain drag pans while shift+drag brushes.
const PAN_STREAM = "[pointerdown[!event.shiftKey], window:pointerup] > window:pointermove!";
const BRUSH_STREAM = "[pointerdown[event.shiftKey], window:pointerup] > window:pointermove!";

export function buildInteractiveSpec(
  spec: VegaLiteSpec,
  options: {
    interactivity: PlotInteractivity;
    armed: boolean;
    domains?: PlotDomains;
    brushColor?: string;
  },
): VegaLiteSpec {
  const { interactivity, armed, domains, brushColor } = options;
  if (interactivity.mode === "static") return spec;
  const next = structuredClone(spec);

  const markRecord = asRecord(next.mark);
  if (typeof next.mark === "string") {
    next.mark = { type: next.mark, tooltip: true };
  } else if (markRecord && markRecord.tooltip === undefined) {
    next.mark = { ...markRecord, tooltip: true };
  }

  if (interactivity.mode !== "full") return next;

  const encoding = asRecord(next.encoding);
  if (encoding && domains) {
    for (const channel of interactivity.channels) {
      const domain = domains[channel];
      const def = asRecord(encoding[channel]);
      if (!domain || !def) continue;
      const scale = asRecord(def.scale) ?? {};
      encoding[channel] = { ...def, scale: { ...scale, domain } };
    }
  }

  if (armed) {
    const params = Array.isArray(next.params) ? next.params : [];
    next.params = [
      ...params,
      {
        name: ZOOM_PARAM,
        select: {
          type: "interval",
          bind: "scales",
          encodings: interactivity.channels,
          translate: PAN_STREAM,
          zoom: "wheel!",
        },
      },
      {
        name: BRUSH_PARAM,
        select: {
          type: "interval",
          encodings: interactivity.channels,
          on: BRUSH_STREAM,
          translate: false,
          zoom: false,
          mark: {
            fill: brushColor ?? "#888888",
            fillOpacity: 0.12,
            stroke: brushColor ?? "#888888",
            strokeOpacity: 0.6,
            strokeDash: [4, 3],
          },
        },
      },
    ];
  }
  return next;
}

// Interval-selection signal values are keyed by field name, e.g.
// { price: [12, 40], date: [1700000000000, 1710000000000] }.
export function selectionDomains(
  value: unknown,
  fields: Partial<Record<ZoomChannel, string>>,
): PlotDomains | null {
  const record = asRecord(value);
  if (!record) return null;
  const domains: PlotDomains = {};
  for (const channel of ["x", "y"] as const) {
    const field = fields[channel];
    if (!field) continue;
    const extent = record[field];
    if (!Array.isArray(extent) || extent.length !== 2) continue;
    const low = Number(extent[0]);
    const high = Number(extent[1]);
    if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) continue;
    domains[channel] = low < high ? [low, high] : [high, low];
  }
  return Object.keys(domains).length > 0 ? domains : null;
}

// Buffers Vega's brush signal (which fires on every pointermove during a
// shift-drag) and only commits the final extent to React once the gesture
// ends. Without this, the domains state update would re-run the embed effect
// mid-drag and abort the gesture before the user releases.
export function makeBrushBuffer(
  fields: Partial<Record<ZoomChannel, string>>,
  commit: (domains: PlotDomains) => void,
): {
  onSignal: (value: unknown) => void;
  onPointerUp: () => void;
} {
  let pending: PlotDomains | null = null;
  return {
    onSignal(value: unknown) {
      const next = selectionDomains(value, fields);
      if (next) pending = next;
    },
    onPointerUp() {
      if (pending === null) return;
      const domains = pending;
      pending = null;
      commit(domains);
    },
  };
}

export function plotPngFilename(title: string | null | undefined): string {
  const base = (title ?? "plot")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${base || "plot"}.png`;
}
