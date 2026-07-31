// Interaction logic for kind:plot artifacts (WIKI-194).
//
// Payloads are arbitrary Vega-Lite specs. Interactivity is injected only into
// single-view specs (a top-level `mark` + `encoding`); composite or malformed
// specs render exactly as before (static fallback). Pan/zoom binds only to
// continuous positional channels that are directly projectable — quantitative
// or temporal, not binned, not aggregated, not timeUnit-transformed.
//
// Design:
//   * ZOOM — one `bind:"scales"` interval per zoomable channel
//     (wiki_zoom_x, wiki_zoom_y). Each param binds its own axis
//     independently, so same-field-both-axes plots still get both scales
//     wired without needing a data-alias workaround.
//   * BRUSH — a single visible interval selection. For distinct-field
//     x+y plots that means a 2D box (encodings: ["x", "y"]). For same-
//     field plots, Vega-Lite dedupes the second projection and the
//     compiled brush mark would render as a full-height band anyway, so
//     we explicitly project only x and let y stay on wheel-zoom — the
//     visible band then matches what actually happens on release.

export type VegaLiteSpec = Record<string, unknown>;
export type ZoomChannel = "x" | "y";
export type PlotDomains = Partial<Record<ZoomChannel, [number, number]>>;

export type PlotInteractivity =
  | { mode: "static" }
  | { mode: "tooltip" }
  | { mode: "full"; channels: ZoomChannel[] };

export const ZOOM_PARAM_PREFIX = "wiki_zoom";
export const BRUSH_PARAM = "wiki_brush";
export const BRUSH_TUPLE_SIGNAL = `${BRUSH_PARAM}_tuple`;

export function zoomParamName(channel: ZoomChannel): string {
  return `${ZOOM_PARAM_PREFIX}_${channel}`;
}

// Every Vega signal, param, and store our injected params generate lives in
// these namespaces. A user-provided param, dataset, or data.name matching
// any prefix would clash with a derived signal (Duplicate signal name) or a
// selection store (source rows replaced with the selection tuple).
const RESERVED_PARAM_PREFIXES = [ZOOM_PARAM_PREFIX, BRUSH_PARAM] as const;

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
  if (def.aggregate) return false;
  if (def.timeUnit) return false;
  if ("scale" in def && def.scale === null) return false;
  const scale = asRecord(def.scale);
  // Vega-Lite preserves an author-owned domainRaw binding. It then ignores a
  // second injected bind:scales domainRaw binding on the same scale, so this
  // axis must not advertise controls that cannot move it.
  if (scale && "domainRaw" in scale) return false;
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
// named `<param>_store`). A user top-level dataset or `data.name` in the
// reserved namespace would be overwritten by the selection tuple on the
// first update — the chart source goes empty.
function datasetNameCollision(spec: Record<string, unknown>): boolean {
  const datasets = asRecord(spec.datasets);
  if (datasets && Object.keys(datasets).some(isReservedName)) return true;
  const data = asRecord(spec.data);
  if (data && typeof data.name === "string" && isReservedName(data.name)) return true;
  return false;
}

// Vega-Lite gives each scale exactly one `domainRaw` binding. If the user's
// spec already has an interval param with `bind: "scales"`, our appended
// wiki_zoom_x / wiki_zoom_y would replace their binding on the same scale —
// their selection still compiles but no longer moves the axis. Non-interval
// selections (point, brush-without-bind) still share compiled signal
// families we don't want to reason about (or clash with by name), so
// downgrade on ANY user param that carries a `select`.
function hasUserSelection(spec: Record<string, unknown>): boolean {
  const params = Array.isArray(spec.params) ? spec.params : [];
  return params.some((param) => {
    const record = asRecord(param);
    return record !== null && "select" in record;
  });
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
  if (hasUserSelection(record)) return { mode: "tooltip" };
  if (hasLegacySelection(record)) return { mode: "tooltip" };
  const mark = markType(record);
  if (mark && COMPOSITE_MARKS.has(mark)) return { mode: "tooltip" };
  return { mode: "full", channels };
}

// Event streams gated on shift so plain drag pans while shift+drag brushes.
const PAN_STREAM = "[pointerdown[!event.shiftKey], window:pointerup] > window:pointermove!";
const BRUSH_STREAM = "[pointerdown[event.shiftKey], window:pointerup] > window:pointermove!";

function encodingField(encoding: Record<string, unknown> | null, channel: ZoomChannel): string | null {
  if (!encoding) return null;
  const def = asRecord(encoding[channel]);
  return typeof def?.field === "string" ? def.field : null;
}

// Which channels the visible brush should project. For distinct-field x+y
// plots that's both channels (2D box). For same-field plots we drop y so
// the compiled brush mark is a proper x-band — Vega-Lite would otherwise
// silently dedupe the y projection and render the brush as a full-height
// band whose visual bounds no longer match what gets zoomed.
export function brushChannels(
  channels: readonly ZoomChannel[],
  encoding: Record<string, unknown> | null,
): ZoomChannel[] {
  if (channels.length < 2) return [...channels];
  const xField = encodingField(encoding, "x");
  const yField = encodingField(encoding, "y");
  if (xField !== null && xField === yField) return ["x"];
  return [...channels];
}

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
    // ZOOM: one bind:scales param per axis. Same-field survives because
    // each param binds a different scale — nothing to dedupe.
    for (const channel of interactivity.channels) {
      injected.push({
        name: zoomParamName(channel),
        // `bind: "scales"` at the parameter level (not inside select) makes
        // Vega-Lite wire domainRaw so drag pans the scale and wheel zooms
        // it. Nested inside select the bind is silently dropped and the
        // same events would draw a selection rectangle instead.
        select: {
          type: "interval",
          encodings: [channel],
          translate: PAN_STREAM,
          zoom: "wheel!",
        },
        bind: "scales",
      });
    }
    // BRUSH: single visible interval. Reader listens to the compiled
    // wiki_brush_tuple signal (channel-tagged) so nested paths / escaped
    // keys / same-field are all handled by the metadata rather than by
    // name lookup.
    injected.push({
      name: BRUSH_PARAM,
      select: {
        type: "interval",
        encodings: brushChannels(interactivity.channels, encoding),
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
    next.params = [...params, ...injected];
  }
  return next;
}

// Vega-Lite emits `<brush>_tuple` as `{fields: [{field, channel, type}, ...],
// values: [[low, high], ...]}` with fields and values ordered identically.
// The channel tag is authoritative — it survives nested field paths (a.b),
// escaped chars, and (in distinct-field mode) both axes cleanly.
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

// Recognizes a signal payload as a well-formed selection tuple even when it
// carries no usable extents (e.g. the user shrank the brush to a point).
// Shape-less noise (null, undefined, primitives, arrays) is left alone so
// unrelated signals don't wipe pending state.
function isWellFormedTuple(value: unknown): boolean {
  const record = asRecord(value);
  return record !== null
    && Array.isArray(record.fields)
    && Array.isArray(record.values);
}

// Buffers Vega's brush tuple (which fires on every pointermove during a
// shift-drag) and only commits the final extent to React once the gesture
// ends. Without this, the domains state update would re-run the embed
// effect mid-drag and abort the gesture before the user releases.
export function makeBrushBuffer(
  channels: readonly ZoomChannel[],
  commit: (domains: PlotDomains) => void,
): {
  onSignal: (value: unknown) => void;
  onPointerUp: () => void;
  onCancel: () => void;
} {
  let pending: PlotDomains | null = null;
  return {
    onSignal(value: unknown) {
      if (!isWellFormedTuple(value)) return;
      // A well-formed tuple that yields no usable extents means the user
      // shrank the brush to a point. Clear pending so a later pointerup
      // doesn't commit an earlier intermediate extent.
      pending = tupleDomains(value, channels);
    },
    onPointerUp() {
      if (pending === null) return;
      const domains = pending;
      pending = null;
      commit(domains);
    },
    // Called on pointercancel and other gesture aborts. Drops whatever
    // extent accumulated so a later unrelated pointerup can't fire a
    // stale zoom.
    onCancel() {
      pending = null;
    },
  };
}

export function plotPngFilename(title: string | null | undefined): string {
  const base = (title ?? "plot")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${base || "plot"}.png`;
}
