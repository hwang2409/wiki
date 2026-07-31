// Interaction logic for kind:plot artifacts (WIKI-194).
//
// Payloads are arbitrary Vega-Lite specs. Interactivity is injected only into
// single-view specs (a top-level `mark` + `encoding`); composite or malformed
// specs render exactly as before (static fallback). Pan/zoom binds only to
// continuous positional channels (quantitative/temporal, un-binned) — ordinal
// axes cannot zoom continuously.

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
  return typeof def.field === "string" && def.field.length > 0 ? def.field : null;
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

export function plotPngFilename(title: string | null | undefined): string {
  const base = (title ?? "plot")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${base || "plot"}.png`;
}
