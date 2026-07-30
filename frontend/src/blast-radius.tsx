import { useEffect, useMemo, useState } from "react";
import { getBlastRadius, type BlastRadiusPayload } from "./api";

const REFRESH_MS = 15_000;

export type BlastRadiusPanelProps = {
  candidate?: string;
  fetchData?: (candidate: string, signal: AbortSignal) => Promise<BlastRadiusPayload>;
  refreshMs?: number;
};

function defaultFetch(candidate: string, signal: AbortSignal) {
  return getBlastRadius(candidate, signal);
}

export function BlastRadiusPanel({
  candidate = "all",
  fetchData = defaultFetch,
  refreshMs = REFRESH_MS,
}: BlastRadiusPanelProps) {
  const [payload, setPayload] = useState<BlastRadiusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const normalizedCandidate = candidate.trim() || "all";

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;

    const load = () => {
      controller = new AbortController();
      fetchData(normalizedCandidate, controller.signal)
        .then((next) => {
          if (cancelled) return;
          setPayload(next);
          setError(null);
          timer = setTimeout(load, refreshMs);
        })
        .catch((reason: unknown) => {
          if (cancelled || (reason instanceof DOMException && reason.name === "AbortError")) return;
          setError(reason instanceof Error ? reason.message : "could not load collision risk");
          timer = setTimeout(load, refreshMs);
        });
    };

    setPayload(null);
    setError(null);
    load();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [fetchData, normalizedCandidate, refreshMs]);

  const collisions = useMemo(
    () =>
      [...(payload?.collisions ?? [])].sort(
        (left, right) =>
          right.overlap_count - left.overlap_count ||
          `${left.left}:${left.right}`.localeCompare(`${right.left}:${right.right}`),
      ),
    [payload],
  );

  return (
    <section className="blast-radius-panel" aria-label="Collision risk" data-testid="blast-radius-panel">
      <div className="blast-radius-header">
        <div>
          <div className="blast-radius-title">collision risk</div>
          <div className="blast-radius-subtitle">
            {normalizedCandidate === "all" ? "active branches" : `candidate ${normalizedCandidate}`}
          </div>
        </div>
        {payload ? (
          <span className={`blast-radius-risk is-${payload.risk.level}`}>
            {payload.risk.count} collision{payload.risk.count === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>

      {error ? <div className="blast-radius-error">{error}</div> : null}
      {!payload && !error ? <div className="blast-radius-muted">checking branch refs...</div> : null}
      {payload?.error ? <div className="blast-radius-muted">{payload.error}</div> : null}

      {payload && collisions.length === 0 ? (
        <div className="blast-radius-empty" data-testid="blast-radius-no-overlap">
          <strong>no overlap</strong>
          <span>No active branch shares changed files with this view.</span>
        </div>
      ) : null}

      {collisions.length > 0 ? (
        <div className="blast-radius-collisions">
          {collisions.map((collision) => (
            <div
              className="blast-radius-collision"
              key={`${collision.left}:${collision.right}`}
              data-testid="blast-radius-collision"
            >
              <div className="blast-radius-collision-head">
                <span>{collision.left}</span>
                <span className="blast-radius-arrow">&lt;-&gt;</span>
                <span>{collision.right}</span>
                <span className="blast-radius-count">{collision.overlap_count} file{collision.overlap_count === 1 ? "" : "s"}</span>
              </div>
              <div className="blast-radius-files">
                {collision.overlap.map((path) => (
                  <code key={path}>{path}</code>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {payload && payload.risk.hot_files.length > 0 ? (
        <div className="blast-radius-hot-files">
          <span>hot files</span>
          {payload.risk.hot_files.map((path) => (
            <code key={path}>{path}</code>
          ))}
        </div>
      ) : null}
    </section>
  );
}
