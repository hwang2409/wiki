// Interaction logic for kind:plot artifacts (WIKI-194).
//
// Payloads are arbitrary Vega-Lite specs. Interactivity is injected only into
// single-view specs (a top-level `mark` + `encoding`); composite or malformed
// specs render exactly as before (static fallback). Pan/zoom binds only to
// continuous positional channels that are directly projectable — quantitative
// or temporal, not binned, not aggregated, not timeUnit-transformed.
//
// Design: independent per-channel interval parameters, one pair (zoom +
// brush) per zoomable channel. Same-field-on-both-axes therefore Just Works
// without rewriting user data or encodings: each parameter has its own store,
// its own signals, its own visual mark — Vega-Lite has nothing to dedupe.

export type VegaLiteSpec = Record<string, unknown>;
export type ZoomChannel = "x" | "y";
export type PlotDomains = Partial<Record<ZoomChannel, [number, number]>>;

export type PlotInteractivity =
  | { mode: "static" }
  | { mode: "tooltip" }
  | { mode: "full"; channels: ZoomChannel[] };

export const ZOOM_PARAM_PREFIX = "wiki_zoom";
export const BRUSH_PARAM_PREFIX = "wiki_brush";

export function zoomParamName(channel: ZoomChannel): string {
  return `${ZOOM_PARAM_PREFIX}_${channel}`;
}

export function brushParamName(channel: ZoomChannel): string {
  return `${BRUSH_PARAM_PREFIX}_${channel}`;
}

// Every Vega signal, param, and store our injected params generate lives in
// this namespace. A user-provided param, dataset, or data.name matching any
// prefix would clash with a derived signal (Duplicate signal name) or a
// selection store (source rows replaced with the selection tuple).
const RESERVED_PARAM_PREFIXES = [ZOOM_PARAM_PREFIX, BRUSH_PARAM_PREFIX] as const;

// Vega-Lite composite marks compile to multiple primitive marks and silently
// strip interval selections. Advertising full mode on these would enable a
// Reset button and hint drag/wheel/shift-drag with no live wiring.
const COMPOSITE_MARKS = new Set(["boxplot", "errorbar", "errorband"]);

const COMPOSITE_KEYS = ["layer", "facet", "concat", "hconcat", "vconcat", "repeat", "spec"];

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function isReservedName(name: string): boolean {
  return RESERVED_PARAM_PREFIXES.some((prefix) => name === prefix || name.startsWith(`${prefix}_`));
}

function markType(spec: Record<string, unknown>): string | null {
  if (typeof spec.mark === "string") return spec.mark;
  const rec = asRecord(spec.mark);
  return typeof rec?.type === "string" ? rec.type : null;
}

function continuousChannel(encoding: Record<string, unknown>, channel: ZoomChannel): boolean {
  const def = asRecord(encoding[channel]);
  if (!def) return false;
  if (def.type !== "quantitative" && def.type !== "temporal") return false;
  if (def.bin) return false;
  // Aggregate encodings can't be interval-projected; timeUnit encodings key
  // the tuple off a compiled name (yearmonth_ts) that isn't the source field.
  if (def.aggregate) return false;
  if (def.timeUnit) return false;
  // scale: null suppresses scale compilation; interval projection would then
  // have no domainRaw to bind to and Vega-Lite throws.
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

// Vega-Lite compiles each interval param into a selection store (dataset
// named `<param>_store`). If a user top-level dataset already uses one of
// those names, the first selection update replaces its rows with the
// selection tuple — the chart goes empty. Guard against that too.
function datasetNameCollision(spec: Record<string, unknown>): boolean {
  const datasets = asRecord(spec.datasets);
  if (datasets && Object.keys(datasets).some(isReservedName)) return true;
  const data = asRecord(spec.data);
  if (data && typeof data.name === "string" && isReservedName(data.name)) return true;
  return false;
}

// Vega-Lite v3-v4 kept selections in a top-level `selection` object. v5+
// accepts either form, but when both a legacy `selection` and new-style
// `params` coexist Vega-Lite compiles ONLY the legacy selections and drops
// every injected param — the inspector would advertise handlers that don't
// exist. Simpler to degrade than to normalize the legacy form.
function hasLegacySelection(spec: Record<string, unknown>): boolean {
  return "selection" in spec;
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
  if (paramNameCollision(record)) return { mode: "tooltip" };
  if (datasetNameCollision(record)) return { mode: "tooltip" };
  if (hasLegacySelection(record)) return { mode: "tooltip" };
  const mark = markType(record);
  if (mark && COMPOSITE_MARKS.has(mark)) return { mode: "tooltip" };
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
    const injected: unknown[] = [];
    for (const channel of interactivity.channels) {
      injected.push({
        name: zoomParamName(channel),
        // `bind: "scales"` at the parameter level (not inside select) makes
        // Vega-Lite wire domainRaw so drag pans the scale and wheel zooms it.
        // Nested inside select the bind is silently dropped and the same
        // events would draw a selection rectangle instead of moving the axis.
        select: {
          type: "interval",
          encodings: [channel],
          translate: PAN_STREAM,
          zoom: "wheel!",
        },
        bind: "scales",
      });
      injected.push({
        name: brushParamName(channel),
        select: {
          type: "interval",
          encodings: [channel],
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
      });
    }
    next.params = [...params, ...injected];
  }
  return next;
}

// A 1D interval-selection signal is `{ <fieldName>: [low, high] }` when a
// range is selected and `{}` when it is empty. We take the first value in
// the object regardless of the key — the key is the compiled field name
// (which can be an escaped nested path) and we never need to interpret it
// because each param projects exactly one channel by construction.
export function extentFromSignal(value: unknown): [number, number] | null {
  const record = asRecord(value);
  if (!record) return null;
  const values = Object.values(record);
  if (values.length === 0) return null;
  const extent = values[0];
  if (!Array.isArray(extent) || extent.length !== 2) return null;
  const low = Number(extent[0]);
  const high = Number(extent[1]);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) return null;
  return low < high ? [low, high] : [high, low];
}

// Recognizes a signal payload as a well-formed selection signal even when it
// carries no usable extent (the empty `{}` Vega emits when the brush shrinks
// back to a point). Shape-less noise (null, undefined, primitives) is left
// alone so unrelated signals don't wipe pending state.
function isWellFormedSignal(value: unknown): boolean {
  return asRecord(value) !== null;
}

// Buffers Vega's per-channel brush signals (which fire on every pointermove
// during a shift-drag) and only commits the final extent to React once the
// gesture ends. Without this, the domains state update would re-run the
// embed effect mid-drag and abort the gesture before the user releases.
//
// Per-channel signals are handled independently: a shift-drag that leaves x
// selected but shrinks y to empty commits `{x: [...]}` on pointerup — the y
// channel's pending gets cleared, x's stays.
export function makeBrushBuffer(
  channels: readonly ZoomChannel[],
  commit: (domains: PlotDomains) => void,
): {
  onChannelSignal: (channel: ZoomChannel, value: unknown) => void;
  onPointerUp: () => void;
  onCancel: () => void;
} {
  const pending: PlotDomains = {};
  const allow = new Set(channels);
  return {
    onChannelSignal(channel: ZoomChannel, value: unknown) {
      if (!allow.has(channel)) return;
      if (!isWellFormedSignal(value)) return;
      const extent = extentFromSignal(value);
      if (extent) {
        pending[channel] = extent;
      } else {
        // Well-formed signal but no usable extent — user shrank the brush
        // back to a point on this channel. Clear its pending so pointerup
        // doesn't commit an earlier intermediate range.
        delete pending[channel];
      }
    },
    onPointerUp() {
      const keys = Object.keys(pending) as ZoomChannel[];
      if (keys.length === 0) return;
      const domains: PlotDomains = {};
      for (const key of keys) {
        domains[key] = pending[key];
        delete pending[key];
      }
      commit(domains);
    },
    // Called on pointercancel and other gesture aborts. Drops whatever
    // extent accumulated so a later unrelated pointerup can't fire a
    // stale zoom.
    onCancel() {
      for (const key of Object.keys(pending) as ZoomChannel[]) {
        delete pending[key];
      }
    },
  };
}

export function plotPngFilename(title: string | null | undefined): string {
  const base = (title ?? "plot")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${base || "plot"}.png`;
}
