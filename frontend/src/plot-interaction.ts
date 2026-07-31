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
// (field-keyed) because the latter drops distinctness when x and y share a
// field and escapes nested paths inconsistently across Vega versions.

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

// Vega-Lite composite marks compile to multiple primitive marks and silently
// strip interval selections: the classifier previously advertised full mode,
// the UI enabled Reset and hinted drag/wheel/shift-drag, and nothing worked.
const COMPOSITE_MARKS = new Set(["boxplot", "errorbar", "errorband"]);

// Synthetic field name added by the aliasing transform so same-field-both-axes
// specs get two distinct field identifiers on the interval projection (Vega-
// Lite dedupes identical projections and drops the second channel).
const Y_ALIAS_FIELD = "__wiki_plot_y_axis__";

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
  // payloads. (The check spans the whole namespace because Vega-Lite compiles
  // a family of derived signals per interval parameter.)
  if (paramNameCollision(record)) return { mode: "tooltip" };
  // Vega-Lite silently strips interval selections from composite marks
  // (boxplot, errorbar, errorband). Advertising drag/wheel/shift-drag on
  // those would give a Reset button and hint text with no live wiring.
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

// Ports Vega-Lite / vega-util's field-path parser: split "a.b" into ["a","b"],
// "a\\.b" into ["a.b"], "a[0]" into ["a","0"], and "a[0].b" into ["a","0","b"].
// Inlined so the module stays clear of a runtime vega-util dep (this module is
// eagerly imported and vega-util would bloat the initial chunk).
function splitFieldPath(path: string): string[] {
  const out: string[] = [];
  const n = path.length;
  let quote: string | null = null;
  let bracket = 0;
  let escaped = "";
  let i = 0;
  let j = 0;
  function push(): void {
    out.push(escaped + path.substring(i, j));
    escaped = "";
    i = j + 1;
  }
  for (i = j = 0; j < n; j += 1) {
    const c = path[j];
    if (c === "\\") {
      escaped += path.substring(i, j);
      j += 1;
      i = j;
    } else if (c === quote) {
      push();
      quote = null;
      bracket = -1;
    } else if (quote) {
      continue;
    } else if (i === bracket && (c === '"' || c === "'")) {
      i = j + 1;
      quote = c;
    } else if (c === "." && !bracket) {
      if (j > i) push();
      else i = j + 1;
    } else if (c === "[") {
      if (j > i) push();
      bracket = i = j + 1;
    } else if (c === "]") {
      if (bracket > 0) push();
      bracket = 0;
      i = j + 1;
    }
  }
  if (j > i) {
    j += 1;
    push();
  }
  return out;
}

// Builds a Vega expression that reads the given Vega-Lite field path from
// `datum`. Bracketed with quoted strings so nested (a.b), escaped literal
// (a\.b), and array-index (a[0]) paths all resolve to the correct value.
function accessExpression(path: string): string {
  return "datum" + splitFieldPath(path).map((segment) => `[${JSON.stringify(segment)}]`).join("");
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

  // Same-field-both-axes: Vega-Lite's interval selection dedupes projections
  // on identical fields, so a spec with x.field === y.field would compile
  // wiki_zoom for x only, drop y's domainRaw, and produce a tuple with just
  // the x extent. Alias the y-channel via a calculate transform so the
  // selection sees two distinct fields.
  if (
    encoding
    && interactivity.channels.includes("x")
    && interactivity.channels.includes("y")
    && encodingField(encoding, "x") === encodingField(encoding, "y")
  ) {
    const originalField = encodingField(encoding, "x")!;
    const priorTransform = Array.isArray(next.transform) ? next.transform : [];
    // accessExpression handles nested (a.b), array-index (a[0]), and
    // escaped-dot (a\.b) field paths — a literal datum[originalField] would
    // fail for those, the aliased value would be undefined for every row,
    // and Vega would drop the data so the plot renders empty.
    next.transform = [
      ...priorTransform,
      { calculate: accessExpression(originalField), as: Y_ALIAS_FIELD },
    ];
    const yDef = asRecord(encoding.y)!;
    if (yDef.axis === null) {
      // Author explicitly hid the axis (axis:null). Do NOT materialize an
      // axis object here — that would reveal it and leak the workaround.
      encoding.y = { ...yDef, field: Y_ALIAS_FIELD };
    } else {
      // Title precedence: existing axis.title > encoding-level title > raw
      // field name. Only add axis.title when neither source already provides
      // a label; otherwise the alias field name would leak to the user.
      const yAxis = asRecord(yDef.axis);
      const hasAxisTitle = yAxis !== null && "title" in yAxis;
      const hasEncodingTitle = "title" in yDef;
      const nextAxis = hasAxisTitle || hasEncodingTitle
        ? (yAxis ?? undefined)
        : { ...(yAxis ?? {}), title: originalField };
      encoding.y = nextAxis === undefined
        ? { ...yDef, field: Y_ALIAS_FIELD }
        : { ...yDef, field: Y_ALIAS_FIELD, axis: nextAxis };
    }
  }

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
// escaped chars, and (with the aliasing transform above) same-field-both-axes.
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

// Recognizes a signal payload as a well-formed tuple even if it carries no
// usable extents (e.g. the user shrank the brush back to a point). A shape-
// less payload (null, undefined, primitive) is treated as noise and left
// alone so unrelated signals don't wipe pending state.
function isWellFormedTuple(value: unknown): boolean {
  const record = asRecord(value);
  return record !== null
    && Array.isArray(record.fields)
    && Array.isArray(record.values);
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
  onCancel: () => void;
} {
  let pending: PlotDomains | null = null;
  return {
    onSignal(value: unknown) {
      if (!isWellFormedTuple(value)) return;
      // A well-formed tuple that yields no usable extents means the user
      // shrank the brush to a point (or dragged back to the anchor). Clear
      // pending so a later pointerup doesn't commit an intermediate extent.
      pending = tupleDomains(value, channels);
    },
    onPointerUp() {
      if (pending === null) return;
      const domains = pending;
      pending = null;
      commit(domains);
    },
    // Called on pointercancel and any other gesture abort. Discards whatever
    // extent had accumulated so a subsequent unrelated pointerup can't fire
    // a stale zoom.
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
