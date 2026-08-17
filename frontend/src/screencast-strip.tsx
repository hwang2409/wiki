import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertTriangle, MessageSquare, UserRound, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { getFleetScreencast, type ScreencastFrame, type ScreencastWorker } from "./api";

const POLL_INTERVAL_MS = 2000;
export const SCREENCAST_VISIBLE_LINES = 20;
export const SCREENCAST_MAX_TICKETS = 32;

// ------------------------------------------------------------ context

type ScreencastRegistry = {
  subscribe: (ticket: string) => () => void;
  registerStrip: (ticket: string, node: HTMLElement | null) => void;
  get: (ticket: string) => ScreencastWorker | null;
  loading: boolean;
};

const EmptyRegistry: ScreencastRegistry = {
  subscribe: () => () => undefined,
  registerStrip: () => undefined,
  get: () => null,
  loading: false,
};

const ScreencastContext = createContext<ScreencastRegistry>(EmptyRegistry);

// ------------------------------------------------------------ hooks

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

/** True when ANY of the passed nodes is intersecting the viewport. */
function useAnyNodeVisible(nodes: Iterable<HTMLElement>): boolean {
  const [visible, setVisible] = useState(true);
  const nodeArrayKey = useMemo(() => {
    const arr: HTMLElement[] = [];
    for (const node of nodes) arr.push(node);
    return arr;
  }, [nodes]);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    if (nodeArrayKey.length === 0) {
      setVisible(false);
      return;
    }
    const seenVisible = new Set<Element>();
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) seenVisible.add(entry.target);
        else seenVisible.delete(entry.target);
      }
      setVisible(seenVisible.size > 0);
    });
    for (const node of nodeArrayKey) observer.observe(node);
    return () => observer.disconnect();
  }, [nodeArrayKey]);
  return visible;
}

// ------------------------------------------------------------ provider

/**
 * Single fleet-wide poller. Views that show worker cards wrap their tree
 * in this and drop <ScreencastStrip> anywhere inside; each strip
 * self-registers and the provider polls the union of registered tickets
 * exactly once per interval. One request per view, not one per card.
 *
 * The poller pauses when the tab is hidden AND when every mounted strip
 * is off-screen, and honours the previous ETag so an unchanged fleet
 * costs a 304 response, not a full JSON payload.
 *
 * Consumer contract: the context value re-identifies whenever ``workers``
 * or ``loading`` changes so a poll response actually re-renders the
 * strips. It is a bug (R2 finding #1) to memoize on anything narrower
 * than ``[workers, loading, ...stable callbacks]``.
 */
export function ScreencastProvider({ children }: { children: ReactNode }) {
  const [subscribers, setSubscribers] = useState<Map<string, number>>(new Map());
  const [strips, setStrips] = useState<Map<string, HTMLElement>>(new Map());
  const [workers, setWorkers] = useState<Map<string, ScreencastWorker>>(new Map());
  const [loading, setLoading] = useState(true);
  const etagRef = useRef<string | null>(null);

  const subscribe = useCallback((ticket: string) => {
    setSubscribers((prev) => {
      const next = new Map(prev);
      next.set(ticket, (next.get(ticket) ?? 0) + 1);
      return next;
    });
    return () => {
      setSubscribers((prev) => {
        const next = new Map(prev);
        const current = next.get(ticket) ?? 0;
        if (current <= 1) next.delete(ticket);
        else next.set(ticket, current - 1);
        return next;
      });
    };
  }, []);

  const registerStrip = useCallback(
    (ticket: string, node: HTMLElement | null) => {
      setStrips((prev) => {
        const next = new Map(prev);
        if (node) next.set(ticket, node);
        else next.delete(ticket);
        return next;
      });
    },
    []
  );

  const tickets = useMemo(() => {
    return [...subscribers.keys()].sort().slice(0, SCREENCAST_MAX_TICKETS);
  }, [subscribers]);
  const ticketsKey = useMemo(() => tickets.join("|"), [tickets]);

  // Ticket-set changes invalidate the cached ETag — the server hashes
  // the full worker set, so an ETag computed for a superset would 304
  // even when a newly-joined ticket has fresh frames the client has
  // never seen.
  useEffect(() => {
    etagRef.current = null;
  }, [ticketsKey]);

  const stripNodes = useMemo(() => [...strips.values()], [strips]);
  const documentVisible = useDocumentVisible();
  const sectionVisible = useAnyNodeVisible(stripNodes);
  const active = documentVisible && sectionVisible && tickets.length > 0;

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller = new AbortController();
      try {
        const response = await fetch(
          `/api/fleet/screencast?${new URLSearchParams(
            tickets.map((t) => ["ticket", t] as [string, string])
          )}`,
          {
            signal: controller.signal,
            headers: etagRef.current ? { "If-None-Match": etagRef.current } : undefined,
          }
        );
        if (cancelled) return;
        if (response.status === 304) {
          // Nothing changed — keep the previous worker map.
        } else if (response.ok) {
          const data = (await response.json()) as {
            workers: ScreencastWorker[];
          };
          const next = new Map<string, ScreencastWorker>();
          for (const worker of data.workers) next.set(worker.ticket, worker);
          setWorkers(next);
          const nextEtag = response.headers.get("etag");
          if (nextEtag) etagRef.current = nextEtag;
        }
        setLoading(false);
      } catch (err) {
        if ((err as { name?: string })?.name !== "AbortError") setLoading(false);
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

  const contextValue = useMemo<ScreencastRegistry>(
    () => ({
      subscribe,
      registerStrip,
      get: (ticket) => workers.get(ticket) ?? null,
      loading,
    }),
    [workers, loading, subscribe, registerStrip]
  );

  return (
    <ScreencastContext.Provider value={contextValue}>
      {children}
    </ScreencastContext.Provider>
  );
}

// ------------------------------------------------------------ strip UI

function frameIcon(kind: ScreencastFrame["kind"]): LucideIcon {
  switch (kind) {
    case "assistant":
      return MessageSquare;
    case "user":
      return UserRound;
    case "tool":
      return Wrench;
    case "marker":
      return AlertTriangle;
    default:
      return MessageSquare;
  }
}

export function ScreencastStrip({
  ticket,
  runId,
}: {
  ticket: string;
  runId?: string | null;
}) {
  const registry = useContext(ScreencastContext);
  const [rootNode, setRootNode] = useState<HTMLElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => registry.subscribe(ticket), [registry, ticket]);
  useEffect(() => {
    registry.registerStrip(ticket, rootNode);
    return () => registry.registerStrip(ticket, null);
  }, [registry, ticket, rootNode]);

  const worker = registry.get(ticket);
  const frames = worker?.frames ?? [];
  const resolvedRunId = worker?.run_id ?? runId ?? null;

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
      ref={setRootNode}
    >
      <div
        className="fleet-screencast-tape"
        ref={scrollRef}
        aria-live="polite"
        aria-label={`recent output for ${ticket}`}
      >
        {empty ? (
          <div className="fleet-screencast-empty">
            {registry.loading
              ? "listening…"
              : resolvedRunId === null
                ? "no live worker"
                : "no recent output"}
          </div>
        ) : (
          frames.map((frame, index) => {
            const FrameIcon = frameIcon(frame.kind);
            return (
              <div
                className="fleet-screencast-line"
                data-kind={frame.kind}
                key={`${index}-${frame.ts ?? ""}`}
              >
                <FrameIcon aria-hidden="true" className="fleet-screencast-glyph" size={14} />
                <span className="fleet-screencast-text">{frame.text}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// -------------- test / non-context usage ----------------------

/** Pure helper: build a batch URL. Exported for the unit test. */
export function buildScreencastUrl(tickets: string[]): string {
  const params = new URLSearchParams(tickets.map((t) => ["ticket", t] as [string, string]));
  return `/api/fleet/screencast?${params}`;
}

export { getFleetScreencast };
