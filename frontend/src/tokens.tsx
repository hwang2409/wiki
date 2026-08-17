import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getTokens, type TokenBucket, type TokensResponse } from "./api";
import { UtilityEmpty, UtilityError, UtilityLoading, UtilityPage } from "./utility-page";
import { DetailCard, DetailRow } from "./primitives";

type Preset = "24h" | "7d" | "30d";
type BucketMode = "hour" | "day";
type Metric = "input" | "output" | "reasoning" | "cached";
type Hover = {
  bucketIndex: number;
  x: number;
  y: number;
};

const UNAVAILABLE_LABEL = "unavailable";
const METRICS_STACKED: Metric[] = ["input", "output", "reasoning"];
const PRESETS: { key: Preset; label: string; bucket: BucketMode; hoursBack: number }[] = [
  { key: "24h", label: "24h", bucket: "hour", hoursBack: 24 },
  { key: "7d", label: "7d", bucket: "hour", hoursBack: 24 * 7 },
  { key: "30d", label: "30d", bucket: "day", hoursBack: 24 * 30 },
];

const SERIES_VARS = [
  "--accent-primary",
  "--accent-success",
  "--accent-warning",
  "--accent-danger",
  "--text-normal",
  "--text-muted",
  "--syntax-string",
  "--syntax-keyword",
];

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

function formatCompact(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function formatBucketLabel(iso: string, mode: BucketMode): string {
  const d = new Date(iso);
  if (mode === "day") {
    // Backend buckets are UTC-floored; formatting locally would shift the
    // label by a day for anyone in a negative UTC offset.
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
  }
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function bucketFull(iso: string, mode: BucketMode): string {
  if (mode === "day") {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    });
  }
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sumSeries(bucket: TokenBucket, metric: Metric, filter: (key: string) => boolean): number {
  let total = 0;
  for (const [key, values] of Object.entries(bucket.series)) {
    if (!filter(key)) continue;
    total += values[metric] ?? 0;
  }
  return total;
}

function seriesTotal(bucket: TokenBucket, key: string, metrics: Metric[]): number {
  const s = bucket.series[key];
  if (!s) return 0;
  return metrics.reduce((acc, m) => acc + (s[m] ?? 0), 0);
}

// The API can omit a metric key from a bucket's series entry when no data
// source reported it (e.g. reasoning tokens for non-thinking models). Any
// bucket that *does* define the key — even at 0 — marks the metric as
// available. This drives the "unavailable" vs "0" distinction that WIKI-178
// established for the cost dashboard.
function metricAvailability(buckets: TokenBucket[]): Record<Metric, boolean> {
  const seen: Record<Metric, boolean> = {
    input: false,
    output: false,
    reasoning: false,
    cached: false,
  };
  for (const bucket of buckets) {
    for (const values of Object.values(bucket.series)) {
      for (const metric of ["input", "output", "reasoning", "cached"] as Metric[]) {
        if (values[metric] !== undefined) seen[metric] = true;
      }
    }
  }
  return seen;
}

function metricDisplay(total: number, available: boolean): string {
  return available ? formatNumber(total) : UNAVAILABLE_LABEL;
}

export function TokensView() {
  const [preset, setPreset] = useState<Preset>("7d");
  const [bucketMode, setBucketMode] = useState<BucketMode>("hour");
  const [cliFilter, setCliFilter] = useState<Set<string>>(new Set());
  const [modelFilter, setModelFilter] = useState<Set<string>>(new Set());
  const [includeCached, setIncludeCached] = useState(false);
  const [data, setData] = useState<TokensResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retryTick, setRetryTick] = useState(0);
  // Mirror `data` in a ref so the poll `.catch` (which closes over stale
  // state) can decide whether to trip the full error state or keep the
  // last-good view + banner. (Round-4 review MEDIUM: any failed poll
  // wiped the chart, even when a valid snapshot was already on screen.)
  const dataRef = useRef<TokensResponse | null>(null);
  // Query key each snapshot was fetched under. Preserving `data` on a
  // failed request only makes sense for the SAME query — after a range
  // or bucket switch, the last-good snapshot describes a different
  // window than the active controls, and rendering it under the new
  // label misrepresents current usage. (Round-5 review HIGH: 7d totals
  // survived a failed 30d load and rendered under the 30d control.)
  const dataQueryKeyRef = useRef<string | null>(null);
  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  function pickPreset(next: Preset) {
    const chosen = PRESETS.find((p) => p.key === next);
    setPreset(next);
    if (chosen) setBucketMode(chosen.bucket);
  }

  const range = useMemo(() => {
    const chosen = PRESETS.find((p) => p.key === preset) ?? PRESETS[1];
    const to = new Date();
    const from = new Date(to.getTime() - chosen.hoursBack * 3600 * 1000);
    return { from: from.toISOString(), to: to.toISOString() };
  }, [preset]);

  useEffect(() => {
    let cancelled = false;
    let retry = 0;
    const queryKey = `${range.from}|${range.to}|${bucketMode}`;

    const load = (showSpinner: boolean) => {
      if (showSpinner) setLoading(true);
      getTokens({ from: range.from, to: range.to, bucket: bucketMode })
        .then((next) => {
          if (cancelled) return;
          setData(next);
          dataQueryKeyRef.current = queryKey;
          setError(null);
          setRefreshFailed(null);
          if (next.refreshing) {
            retry = window.setTimeout(() => load(false), 1000);
          }
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          const msg = err instanceof Error ? err.message : "Could not load token usage";
          // Preserve last-good data ONLY when the failing request was for
          // the query currently bound to that snapshot — a retry or a
          // background poll of the same range/bucket. After a preset or
          // bucket switch, showing stale numbers under the new control
          // misrepresents current usage; trip the full error state and
          // drop the mismatched data instead.
          if (dataRef.current && dataQueryKeyRef.current === queryKey) {
            setRefreshFailed(msg);
          } else {
            setData(null);
            dataQueryKeyRef.current = null;
            setRefreshFailed(null);
            setError(msg);
          }
        })
        .finally(() => {
          if (!cancelled && showSpinner) setLoading(false);
        });
    };

    load(true);
    return () => {
      cancelled = true;
      if (retry) window.clearTimeout(retry);
    };
  }, [range.from, range.to, bucketMode, retryTick]);

  const retry = useCallback(() => {
    // Keep any last-good `data` so the chart stays on screen while the
    // retry request is in flight — clearing it would collapse to the
    // loading state again and defeat the stale-data banner.
    setError(null);
    setRefreshFailed(null);
    setRetryTick((tick) => tick + 1);
  }, []);

  const activeMetrics: Metric[] = includeCached
    ? [...METRICS_STACKED, "cached"]
    : METRICS_STACKED;

  const filteredBuckets = useMemo(() => {
    if (!data) return [] as TokenBucket[];
    const bucketsFiltered = data.buckets.map((bucket) => {
      const series: TokenBucket["series"] = {};
      for (const [key, values] of Object.entries(bucket.series)) {
        // Only the first "/" separates cli from model — models like
        // "openai/gpt-5.4" would otherwise be truncated by split("/").
        const slash = key.indexOf("/");
        const cli = slash === -1 ? key : key.slice(0, slash);
        const model = slash === -1 ? "" : key.slice(slash + 1);
        if (cliFilter.size > 0 && !cliFilter.has(cli)) continue;
        if (modelFilter.size > 0 && !modelFilter.has(model)) continue;
        series[key] = values;
      }
      return { ts: bucket.ts, series };
    });
    return bucketsFiltered;
  }, [data, cliFilter, modelFilter]);

  const visibleSeriesKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const bucket of filteredBuckets) {
      for (const key of Object.keys(bucket.series)) keys.add(key);
    }
    return Array.from(keys).sort();
  }, [filteredBuckets]);

  const totals = useMemo(() => {
    const acc: Record<Metric, number> = { input: 0, output: 0, reasoning: 0, cached: 0 };
    for (const bucket of filteredBuckets) {
      for (const values of Object.values(bucket.series)) {
        for (const metric of ["input", "output", "reasoning", "cached"] as Metric[]) {
          acc[metric] += values[metric] ?? 0;
        }
      }
    }
    return acc;
  }, [filteredBuckets]);

  const availability = useMemo(() => metricAvailability(filteredBuckets), [filteredBuckets]);

  const seriesColor = useMemo(() => {
    const colors: Record<string, string> = {};
    visibleSeriesKeys.forEach((key, i) => {
      const varName = SERIES_VARS[i % SERIES_VARS.length];
      colors[key] = cssVar(varName, "#888");
    });
    return colors;
  }, [visibleSeriesKeys]);

  const hasAnyData = filteredBuckets.some((bucket) => Object.keys(bucket.series).length > 0);

  const subtitle = data ? (
    <>
      {formatNumber(data.sessions_scanned)} sessions scanned
      {data.refreshing ? <span className="tokens-refreshing-inline"> · refreshing</span> : null}
    </>
  ) : (
    "Aggregated token spend across recent agent sessions."
  );

  return (
    <UtilityPage
      title="Token usage"
      subtitle={subtitle}
      scroll={false}
      actions={
        <div className="tokens-preset-group" role="group" aria-label="Time range">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              className={`tokens-chip${preset === p.key ? " is-active" : ""}`}
              type="button"
              onClick={() => pickPreset(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
      }
    >
      <div className="tokens-view">
        {error ? (
          <div className="tokens-error-state">
            <UtilityError
              title="Token usage is unavailable"
              message={error}
              onRetry={retry}
            />
          </div>
        ) : (
        <>
        {refreshFailed ? (
          <div
            className="tokens-refresh-banner"
            role="status"
            aria-live="polite"
          >
            <span className="tokens-refresh-message">
              Showing the last loaded snapshot. Refresh failed: {refreshFailed}
            </span>
            <button
              className="tokens-refresh-retry"
              type="button"
              onClick={retry}
            >
              Retry
            </button>
          </div>
        ) : null}
        <div className="tokens-controls">
          <div className="tokens-bucket-group" role="group" aria-label="Bucket size">
            {(["hour", "day"] as BucketMode[]).map((mode) => (
              <button
                key={mode}
                className={`tokens-chip${bucketMode === mode ? " is-active" : ""}`}
                type="button"
                onClick={() => setBucketMode(mode)}
              >
                {mode}
              </button>
            ))}
          </div>
          <label className="tokens-cached-toggle">
            <input
              type="checkbox"
              checked={includeCached}
              onChange={(e) => setIncludeCached(e.target.checked)}
            />
            <span>include cached</span>
          </label>
        </div>

        {data && (data.clis.length > 0 || data.models.length > 0) ? (
          <div className="tokens-controls tokens-filter-row">
            {data.clis.length > 0 ? (
              <div className="tokens-filter-group" aria-label="Agent filter">
                <span className="tokens-filter-label">agent</span>
                {data.clis.map((cli) => {
                  const active = cliFilter.has(cli);
                  return (
                    <button
                      key={cli}
                      className={`tokens-chip${active ? " is-active" : ""}`}
                      type="button"
                      onClick={() =>
                        setCliFilter((current) => {
                          const next = new Set(current);
                          if (active) next.delete(cli);
                          else next.add(cli);
                          return next;
                        })
                      }
                    >
                      {cli}
                    </button>
                  );
                })}
              </div>
            ) : null}
            {data.models.length > 0 ? (
              <div className="tokens-filter-group" aria-label="Model filter">
                <span className="tokens-filter-label">model</span>
                {data.models.map((model) => {
                  const active = modelFilter.has(model);
                  return (
                    <button
                      key={model}
                      className={`tokens-chip${active ? " is-active" : ""}`}
                      type="button"
                      onClick={() =>
                        setModelFilter((current) => {
                          const next = new Set(current);
                          if (active) next.delete(model);
                          else next.add(model);
                          return next;
                        })
                      }
                    >
                      {model}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </div>
        ) : null}

        <DetailCard className="tokens-totals" role="group" aria-label="Token totals">
          <DetailRow label="input">
            <TotalCell total={totals.input} available={availability.input} />
          </DetailRow>
          <DetailRow label="output">
            <TotalCell total={totals.output} available={availability.output} />
          </DetailRow>
          <DetailRow label="reasoning">
            <TotalCell total={totals.reasoning} available={availability.reasoning} />
          </DetailRow>
          <DetailRow label="cached">
            <TotalCell total={totals.cached} available={availability.cached} secondary />
          </DetailRow>
          <DetailRow label="sessions">
            <span className="tokens-total-value tabular-nums">
              {data ? formatNumber(data.sessions_scanned) : "—"}
            </span>
          </DetailRow>
        </DetailCard>

        {loading && !data ? (
          <div className="tokens-chart tokens-chart-state bb-detail-card">
            <UtilityLoading label="Reading token telemetry…" lines={[70, 90, 60, 84]} />
          </div>
        ) : !hasAnyData ? (
          <div className="tokens-chart tokens-chart-state bb-detail-card">
            <UtilityEmpty
              title={
                cliFilter.size > 0 || modelFilter.size > 0
                  ? "No usage matches these filters"
                  : "No token usage in this range"
              }
              message={
                cliFilter.size > 0 || modelFilter.size > 0
                  ? "Clear the filters or widen the time range."
                  : "Widen the time range or run an agent session to populate this view."
              }
            />
          </div>
        ) : (
          <TokensChart
            buckets={filteredBuckets}
            bucketMode={bucketMode}
            seriesKeys={visibleSeriesKeys}
            seriesColor={seriesColor}
            activeMetrics={activeMetrics}
          />
        )}

        <div className="tokens-legend">
          {visibleSeriesKeys.map((key) => (
            <span key={key} className="tokens-legend-item">
              <span className="tokens-legend-swatch" style={{ background: seriesColor[key] }} />
              <span className="tokens-legend-name">{key}</span>
            </span>
          ))}
        </div>
        </>
        )}
      </div>
    </UtilityPage>
  );
}

function TotalCell({
  total,
  available,
  secondary = false,
}: {
  total: number;
  available: boolean;
  secondary?: boolean;
}) {
  return (
    <div className={`tokens-total${secondary ? " tokens-total-secondary" : ""}`}>
      <span
        className={`tokens-total-value tabular-nums${available ? "" : " is-unavailable"}`}
        title={available ? undefined : "This metric was not reported for the current selection."}
      >
        {metricDisplay(total, available)}
      </span>
    </div>
  );
}

function TokensChart({
  buckets,
  bucketMode,
  seriesKeys,
  seriesColor,
  activeMetrics,
}: {
  buckets: TokenBucket[];
  bucketMode: BucketMode;
  seriesKeys: string[];
  seriesColor: Record<string, string>;
  activeMetrics: Metric[];
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const observer = new ResizeObserver(() => {
      const rect = wrap.getBoundingClientRect();
      setDims({ w: rect.width, h: rect.height });
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  const layout = useMemo(() => {
    const padding = { top: 20, right: 12, bottom: 32, left: 56 };
    const chartWidth = Math.max(0, dims.w - padding.left - padding.right);
    const chartHeight = Math.max(0, dims.h - padding.top - padding.bottom);
    const barCount = buckets.length || 1;
    const gap = 2;
    const barSlot = chartWidth / barCount;
    const barWidth = Math.max(1, barSlot - gap);
    const maxValue = buckets.reduce((acc, bucket) => {
      const total = seriesKeys.reduce(
        (sum, key) => sum + seriesTotal(bucket, key, activeMetrics),
        0,
      );
      return Math.max(acc, total);
    }, 0);
    return { padding, chartWidth, chartHeight, barSlot, barWidth, maxValue };
  }, [buckets, seriesKeys, activeMetrics, dims.w, dims.h]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || dims.w === 0 || dims.h === 0) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(dims.w * dpr);
    canvas.height = Math.round(dims.h * dpr);
    canvas.style.width = `${dims.w}px`;
    canvas.style.height = `${dims.h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, dims.w, dims.h);

    const textFaint = cssVar("--text-faint", "#999");
    const textMuted = cssVar("--text-muted", "#777");
    const border = cssVar("--background-modifier-border", "#eee");

    const { padding, chartWidth, chartHeight, barSlot, barWidth, maxValue } = layout;

    // Gridlines + y-axis labels.
    ctx.strokeStyle = border;
    ctx.fillStyle = textFaint;
    ctx.font =
      '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const gridSteps = 4;
    for (let i = 0; i <= gridSteps; i += 1) {
      const y = padding.top + (chartHeight * i) / gridSteps;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + chartWidth, y);
      ctx.stroke();
      const value = maxValue * (1 - i / gridSteps);
      ctx.fillText(formatCompact(value), padding.left - 6, y);
    }

    if (maxValue === 0) return;

    // Stacked bars.
    buckets.forEach((bucket, idx) => {
      let stackTop = padding.top + chartHeight;
      const x = padding.left + idx * barSlot;
      for (const key of seriesKeys) {
        const value = seriesTotal(bucket, key, activeMetrics);
        if (value <= 0) continue;
        const h = (value / maxValue) * chartHeight;
        stackTop -= h;
        ctx.fillStyle = seriesColor[key] ?? textMuted;
        ctx.fillRect(x, stackTop, barWidth, h);
      }
    });

    // X-axis ticks — thin them out when there are many buckets.
    const tickStep = Math.max(1, Math.ceil(buckets.length / 8));
    ctx.fillStyle = textMuted;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    buckets.forEach((bucket, idx) => {
      if (idx % tickStep !== 0) return;
      const x = padding.left + idx * barSlot + barWidth / 2;
      ctx.fillText(
        formatBucketLabel(bucket.ts, bucketMode),
        x,
        padding.top + chartHeight + 6,
      );
    });
  }, [buckets, seriesKeys, seriesColor, activeMetrics, bucketMode, dims.w, dims.h, layout]);

  function onMouseMove(event: React.MouseEvent<HTMLDivElement>) {
    if (buckets.length === 0) return;
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const { padding, chartWidth, barSlot } = layout;
    if (x < padding.left || x > padding.left + chartWidth) {
      setHover(null);
      return;
    }
    const idx = Math.min(buckets.length - 1, Math.max(0, Math.floor((x - padding.left) / barSlot)));
    setHover({ bucketIndex: idx, x, y });
  }

  function onMouseLeave() {
    setHover(null);
  }

  const hoveredBucket = hover ? buckets[hover.bucketIndex] : null;

  return (
    <div
      ref={wrapRef}
      className="tokens-chart bb-detail-card"
      onMouseMove={onMouseMove}
      onMouseLeave={onMouseLeave}
    >
      <canvas ref={canvasRef} />
      {hoveredBucket ? (
        <div
          className="tokens-tooltip"
          style={{
            left: Math.min(hover!.x + 12, dims.w - 220),
            top: Math.max(0, hover!.y - 12),
          }}
        >
          <div className="tokens-tooltip-title">{bucketFull(hoveredBucket.ts, bucketMode)}</div>
          {seriesKeys.map((key) => {
            const value = seriesTotal(hoveredBucket, key, activeMetrics);
            if (value <= 0) return null;
            return (
              <div key={key} className="tokens-tooltip-row">
                <span
                  className="tokens-tooltip-swatch"
                  style={{ background: seriesColor[key] }}
                />
                <span className="tokens-tooltip-name">{key}</span>
                <span className="tokens-tooltip-value tabular-nums">
                  {formatNumber(value)}
                </span>
              </div>
            );
          })}
          <div className="tokens-tooltip-sep" />
          {(["input", "output", "reasoning", "cached"] as Metric[]).map((metric) => {
            const bucketAvail = ["input", "output", "reasoning", "cached"].reduce(
              (acc, m) => {
                acc[m as Metric] = Object.values(hoveredBucket.series).some(
                  (v) => v[m as Metric] !== undefined,
                );
                return acc;
              },
              {} as Record<Metric, boolean>,
            );
            const total = sumSeries(hoveredBucket, metric, () => true);
            return (
              <div key={metric} className="tokens-tooltip-row is-secondary">
                <span className="tokens-tooltip-name">{metric}</span>
                <span
                  className={`tokens-tooltip-value tabular-nums${
                    bucketAvail[metric] ? "" : " is-unavailable"
                  }`}
                >
                  {metricDisplay(total, bucketAvail[metric])}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
