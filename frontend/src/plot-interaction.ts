// Interaction logic for kind:plot artifacts (WIKI-194).
//
// Payloads are arbitrary Vega-Lite specs. Interactivity is injected only into
// single-view specs (a top-level `mark` + `encoding`); composite or malformed
// specs render exactly as before (static fallback). Pan/zoom binds only to
// continuous positional channels that are directly projectable — quantitative
// or temporal, not binned, not aggregated, not timeUnit-transformed.
//
// Brush selection extents are read from the compiled `wiki_brush_tuple`
// signal (channel-tagged) rather than the user-facing `wiki_brush` signal
// (which keys by field name, so nested paths get escaped inconsistently and
// same-field-both-axes collapses to a single entry).

export type VegaLiteSpec = Record<string, unknown>;
export type ZoomChannel = "x" | "y";
export type PlotDomains = Partial<Record<ZoomChannel, [number, number]>>;

export type PlotInteractivity =
  | { mode: "static" }
  | { mode: "tooltip" }
  | { mode: "full"; channels: ZoomChannel[] };

export const ZOOM_PARAM = "wiki_zoom";
export const BRUSH_PARAM = "wiki_brush";
export const BRUSH_TUPLE_SIGNAL = `${BRUSH_PARAM}_tuple`;

// Every Vega signal that our injected wiki_zoom / wiki_brush params generate.
// Any user param whose name matches one of these (or starts with the prefix +
// underscore) would collide at parse time with "Duplicate signal name".
const RESERVED_PARAM_PREFIXES = [ZOOM_PARAM, BRUSH_PARAM] as const;

const COMPOSITE_KEYS = ["layer", "facet", "concat", "hconcat", "vconcat", "repeat", "spec"];

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function isReservedName(name: string): boolean {
  return RESERVED_PARAM_PREFIXES.some((prefix) => name === prefix || name.startsWith(`${prefix}_`));
}

function continuousChannel(encoding: Record<string, unknown>, channel: ZoomChannel): boolean {
  const def = asRecord(encoding[channel]);
  if (!def) return false;
  if (def.type !== "quantitative" && def.type !== "temporal") return false;
  if (def.bin) return false;
  // Aggregate encodings can't be interval-projected in Vega-Lite; timeUnit
  // encodings key the tuple off a compiled name (e.g. yearmonth_ts) that
  // isn't the source field, so downstream domain application would break.
  if (def.aggregate) return false;
  if (def.timeUnit) return false;
  // scale: null suppresses scale compilation for the channel; interval
  // projection would then have no domainRaw to bind to and Vega-Lite throws
  // "Cannot read properties of undefined (reading get)".
  if ("scale" in def && def.scale === null) return false;
  return typeof def.field === "string" && def.field.length > 0;
}

function paramNameCollision(spec: Record<string, unknown>): boolean {
  const params = Array.isArray(spec.params) ? spec.params : [];
  return params.some((param) => {
    const record = asRecord(param);
    return typeof record?.name === "string" && isReservedName(record.name);
  });
}

export function plotInteractivity(spec: unknown): PlotInteractivity {
  const record = asRecord(spec);
  if (!record || !("mark" in record)) return { mode: "static" };
  if (COMPOSITE_KEYS.some((key) => key in record)) return { mode: "static" };
  const encoding = asRecord(record.encoding);
  if (!encoding) return { mode: "static" };
  const channels: ZoomChannel[] = [];
  for (const channel of ["x", "y"] as const) {
    if (continuousChannel(encoding, channel)) channels.push(channel);
  }
  if (channels.length === 0) return { mode: "tooltip" };
  // A spec that already reserves any wiki_zoom* or wiki_brush* signal name
  // would trip Vega's duplicate-signal check on inject. Degrade to tooltip so
  // the plot still renders — WIKI-194 must not regress previously-working
  // payloads. (Note the check spans the whole namespace, not just the exact
  // parameter names, because Vega-Lite compiles a family of derived signals
  // per interval parameter.)
  if (paramNameCollision(record)) return { mode: "tooltip" };
  return { mode: "full", channels };
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
        // `bind: "scales"` lives at the parameter level in Vega-Lite v5/v6.
        // Nested inside `select` it is silently ignored — the compiled scales
        // get no domainRaw signal, and drag draws a rectangle instead of
        // panning, wheel resizes the rectangle instead of zooming.
        select: {
          type: "interval",
          encodings: interactivity.channels,
          translate: PAN_STREAM,
          zoom: "wheel!",
        },
        bind: "scales",
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

// Vega-Lite emits `<brush>_tuple` as `{fields: [{field, channel, type}, ...],
// values: [[low, high], ...]}` with fields and values ordered identically.
// The channel tag is authoritative — it survives nested field paths (a.b),
// escaped chars, and the same field appearing on both axes (which the
// user-facing `wiki_brush` signal would collapse to a single key).
export function tupleDomains(
  value: unknown,
  channels: readonly ZoomChannel[],
): PlotDomains | null {
  const record = asRecord(value);
  if (!record) return null;
  const fields = Array.isArray(record.fields) ? record.fields : null;
  const values = Array.isArray(record.values) ? record.values : null;
  if (!fields || !values || fields.length !== values.length) return null;
  const allow = new Set(channels);
  const domains: PlotDomains = {};
  for (let i = 0; i < fields.length; i += 1) {
    const meta = asRecord(fields[i]);
    if (!meta) continue;
    const channel = meta.channel;
    if (channel !== "x" && channel !== "y") continue;
    if (!allow.has(channel)) continue;
    const extent = values[i];
    if (!Array.isArray(extent) || extent.length !== 2) continue;
    const low = Number(extent[0]);
    const high = Number(extent[1]);
    if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) continue;
    domains[channel] = low < high ? [low, high] : [high, low];
  }
  return Object.keys(domains).length > 0 ? domains : null;
}

// Buffers Vega's brush tuple (which fires on every pointermove during a
// shift-drag) and only commits the final extent to React once the gesture
// ends. Without this, the domains state update would re-run the embed effect
// mid-drag and abort the gesture before the user releases.
export function makeBrushBuffer(
  channels: readonly ZoomChannel[],
  commit: (domains: PlotDomains) => void,
): {
  onSignal: (value: unknown) => void;
  onPointerUp: () => void;
} {
  let pending: PlotDomains | null = null;
  return {
    onSignal(value: unknown) {
      const next = tupleDomains(value, channels);
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
