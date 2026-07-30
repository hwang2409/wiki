import { useEffect, useMemo, useRef, useState } from "react";
import { getFleetScreencast, type ScreencastFrame, type ScreencastWorker } from "./api";

const POLL_INTERVAL_MS = 2000;
const VISIBLE_LINES = 6;

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() =>
    typeof document === "undefined" ? true : document.visibilityState !== "hidden"
  );
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

function useSectionVisible(node: HTMLElement | null): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) setVisible(entry.isIntersecting);
      },
      { threshold: 0 }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [node]);
  return visible;
}

/** Batched fleet screencast poll. One request covers all visible tickets. */
export function useFleetScreencast(
  tickets: string[],
  { active }: { active: boolean }
): Map<string, ScreencastWorker> {
  const [byTicket, setByTicket] = useState<Map<string, ScreencastWorker>>(new Map());
  const ticketsKey = useMemo(() => [...tickets].sort().join("|"), [tickets]);

  useEffect(() => {
    if (!active || tickets.length === 0) return;
    let cancelled = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller = new AbortController();
      try {
        const data = await getFleetScreencast(tickets, controller.signal);
        if (cancelled) return;
        const next = new Map<string, ScreencastWorker>();
        for (const worker of data.workers) next.set(worker.ticket, worker);
        setByTicket(next);
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
      }
      if (cancelled) return;
      timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      controller?.abort();
    };
  }, [ticketsKey, active]);

  return byTicket;
}

function frameGlyph(kind: ScreencastFrame["kind"]): string {
  switch (kind) {
    case "assistant":
      return "»";
    case "user":
      return "‹";
    case "tool":
      return "·";
    case "marker":
      return "!";
    default:
      return " ";
  }
}

export function ScreencastStrip({
  ticket,
  worker,
  loading,
}: {
  ticket: string;
  worker: ScreencastWorker | null;
  loading: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const frames = worker?.frames ?? [];
  const runId = worker?.run_id ?? null;

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [frames]);

  const empty = frames.length === 0;
  return (
    <div
      className="fleet-screencast"
      data-ticket={ticket}
      data-empty={empty ? "true" : "false"}
    >
      <div
        className="fleet-screencast-tape"
        ref={scrollRef}
        style={{ ["--fleet-screencast-lines" as string]: VISIBLE_LINES }}
        aria-live="polite"
        aria-label={`recent output for ${ticket}`}
      >
        {empty ? (
          <div className="fleet-screencast-empty">
            {loading
              ? "listening…"
              : runId === null
                ? "no live worker"
                : "no recent output"}
          </div>
        ) : (
          frames.map((frame, index) => (
            <div
              className="fleet-screencast-line"
              data-kind={frame.kind}
              key={`${index}-${frame.ts ?? ""}`}
            >
              <span className="fleet-screencast-glyph" aria-hidden="true">
                {frameGlyph(frame.kind)}
              </span>
              <span className="fleet-screencast-text">{frame.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/** Container that pauses polling when the section is off-screen or tab hidden. */
export function FleetScreencastPanel({
  tickets,
  tickets_meta,
}: {
  tickets: string[];
  tickets_meta: Array<{ ticket: string; state: string; role: string }>;
}) {
  const [rootNode, setRootNode] = useState<HTMLElement | null>(null);
  const documentVisible = useDocumentVisible();
  const sectionVisible = useSectionVisible(rootNode);
  const active = documentVisible && sectionVisible;
  const [firstResponse, setFirstResponse] = useState(false);
  const byTicket = useFleetScreencast(tickets, { active });

  useEffect(() => {
    if (byTicket.size > 0) setFirstResponse(true);
  }, [byTicket]);

  return (
    <div className="fleet-screencast-panel" ref={setRootNode}>
      {tickets_meta.map((meta) => (
        <article
          className="fleet-screencast-card"
          data-state={meta.state}
          key={meta.ticket}
        >
          <header className="fleet-screencast-card-head">
            <span className="fleet-screencast-ticket">{meta.ticket}</span>
            <span className="fleet-screencast-meta">
              {meta.role} · {meta.state}
            </span>
          </header>
          <ScreencastStrip
            ticket={meta.ticket}
            worker={byTicket.get(meta.ticket) ?? null}
            loading={!firstResponse && active}
          />
        </article>
      ))}
    </div>
  );
}
