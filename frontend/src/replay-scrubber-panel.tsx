import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertTriangle,
  Flag,
  MessageSquare,
  Pause,
  Play,
  SkipBack,
  SkipForward,
} from "lucide-react";
import {
  getAgentReplayRuns,
  getReplayRawEvent,
  getReplayTimeline,
  type ReplayBookmark,
  type ReplayBookmarkKind,
  type ReplayRawEvent,
  type ReplayRunSummary,
  type ReplayTimeline,
  type ReplayTimelineEvent,
  type SessionEvent,
} from "./api";
import { CopyPill } from "./copy-button";
import { providerEventPresentation, type PresentationBlock } from "./replay-event-adapter";
import { BoundedPreview } from "./transcript-preview";
import { SessionMarkdown, ToolCallRow } from "./session";

const SPEED_OPTIONS = [0.5, 1, 2, 5, 10] as const;
const MAX_SPEED_LABEL = "max";
type SpeedChoice = (typeof SPEED_OPTIONS)[number] | typeof MAX_SPEED_LABEL;

const MAX_ADVANCE_DELAY_MS = 10_000;
const MIN_ADVANCE_DELAY_MS = 40;

// Bounded event window (round-4 review item 2): the panel keeps at most
// ``windowSize`` events in memory at any moment. When a new page arrives
// and puts us over the cap, the OLDEST events evict from the front.
// Playback and scrubbing prefetch the next page only as we approach the
// window's forward edge, so a 100k-event run does not accumulate 100k JS
// objects. Exported as a MUTABLE object so tests can force eviction with
// a small window without inflating the fixture size.
export const REPLAY_TUNABLES = {
  windowSize: 5_000,
  prefetchMargin: 128,
  pageSize: 500,
};

function formatClockTime(iso: string | null): string {
  if (!iso) return "--:--:--";
  return iso.slice(11, 19) || iso;
}

function formatRunLabel(run: ReplayRunSummary): string {
  const when = run.updated_at || run.created_at || "";
  const label = when.slice(0, 19).replace("T", " ");
  const bits = [run.role, run.provider].filter(Boolean).join(" · ");
  return `${label}${bits ? ` — ${bits}` : ""}`;
}

function eventDelay(
  events: ReplayTimelineEvent[],
  index: number,
  speed: SpeedChoice,
): number {
  if (speed === MAX_SPEED_LABEL) return MIN_ADVANCE_DELAY_MS;
  const current = events[index];
  const next = events[index + 1];
  if (!current?.ts || !next?.ts) return 350;
  const delta = Date.parse(next.ts) - Date.parse(current.ts);
  if (!Number.isFinite(delta) || delta <= 0) return MIN_ADVANCE_DELAY_MS;
  const scaled = delta / (speed as number);
  return Math.min(Math.max(scaled, MIN_ADVANCE_DELAY_MS), MAX_ADVANCE_DELAY_MS);
}

function bookmarkIcon(kind: ReplayBookmarkKind) {
  if (kind === "steer") return <MessageSquare size={11} />;
  if (kind === "verdict") return <Flag size={11} />;
  return <AlertTriangle size={11} />;
}

function bookmarkTitle(bookmark: ReplayBookmark): string {
  const clock = formatClockTime(bookmark.ts);
  return `${bookmark.kind} @ ${clock} · ${bookmark.summary}`;
}

const DISPOSITION_LABELS: Record<string, string> = {
  rendered: "shown",
  summarized: "summarized",
  ignored: "skipped",
  intentionally_ignored: "skipped",
  unknown: "unclassified",
};

const LIFECYCLE_LABELS: Record<string, string> = {
  working: "working",
  idle: "idle",
  blocked: "blocked",
  interrupted: "interrupted",
  completed: "complete",
  inProgress: "in progress",
  in_progress: "in progress",
};

const BOOKMARK_LABELS: Record<string, string> = {
  steer: "steer",
  verdict: "verdict",
  error: "error",
};

const EVENT_KIND_LABELS: Record<string, string> = {
  claude_client_message: "client message",
  claude_hook_response: "hook response",
  claude_hook_started: "hook started",
  claude_init: "session started",
  claude_rate_limit_event: "rate limit update",
  claude_result: "turn result",
  claude_status: "status update",
  claude_stream_event: "stream update",
  codex_client_message: "client message",
  item_completed: "item completed",
  item_started: "item started",
  provider_process_exit: "provider stopped",
  provider_stderr: "provider error output",
  turn_completed: "turn completed",
  turn_started: "turn started",
  warning: "warning",
};

function plainEnum(value: string | null, labels: Record<string, string>): string | null {
  if (!value) return null;
  return labels[value] ?? value;
}

function plainEventLabel(event: ReplayTimelineEvent): string {
  const label = EVENT_KIND_LABELS[event.kind] ?? (event.summary || event.kind);
  const lifecycle = plainEnum(event.lifecycle_state, LIFECYCLE_LABELS);
  return lifecycle ? `${label} · ${lifecycle}` : label;
}

function ReplayMessageSummary({ role, text }: { role: "user" | "assistant"; text: string }) {
  return (
    <BoundedPreview
      className={`replay-event-message is-${role}`}
      previewLines={6}
      showSummary={false}
      text={text}
      variant="block"
      renderBody={({ text: previewText }) => (
        <SessionMarkdown className="replay-event-message-markdown" text={previewText} />
      )}
    />
  );
}

function ReplayToolOrMarker({
  event,
  marker,
  tool,
  ticket,
}: {
  event: ReplayTimelineEvent;
  marker?: string;
  tool: NonNullable<SessionEvent["tool"]> | null;
  ticket: string;
}) {
  if (marker) return <div className="replay-event-marker">{marker}</div>;
  if (!tool) {
    return <div className="replay-event-marker">{plainEventLabel(event)}</div>;
  }
  const toolEvent: SessionEvent = {
    id: event.seq,
    kind: "tool",
    ts: event.ts,
    text: "",
    disposition: "rendered",
    tool,
  };
  return <ToolCallRow event={toolEvent} ticket={ticket} withResult={false} />;
}

function ReplayPresentationBlocks({
  blocks,
  event,
  ticket,
}: {
  blocks: PresentationBlock[];
  event: ReplayTimelineEvent;
  ticket: string;
}) {
  return (
    <>
      {blocks.map((block, index) => {
        if (block.type === "message") {
          return <ReplayMessageSummary key={`message:${index}`} role={block.role} text={block.text} />;
        }
        if (block.type === "tool") {
          return <ReplayToolOrMarker key={`tool:${index}`} event={event} ticket={ticket} tool={block.tool} />;
        }
        const marker = EVENT_KIND_LABELS[event.kind] ? plainEventLabel(event) : block.text;
        return <ReplayToolOrMarker key={`marker:${index}`} event={event} marker={marker} ticket={ticket} tool={null} />;
      })}
    </>
  );
}

/**
 * Rolling window over a paginated event stream.
 *
 * ``droppedFromFront`` is the count of events evicted since we started
 * loading the first page. It converts between the panel's "absolute index"
 * (0-based over the run's full sequence) and the position inside the
 * currently-held ``events`` array. When the user scrubs backward past the
 * evicted edge, the loader resets and re-pages forward — the round-3
 * code accumulated every event ever fetched into a single array and hit
 * memory pressure on long runs.
 */
type TimelineWindow = {
  run: ReplayRunSummary;
  events: ReplayTimelineEvent[];
  droppedFromFront: number;
  hasMore: boolean;
  nextCursor: string | null;
  bookmarks: ReplayBookmark[];
  bookmarksTruncated: boolean;
  warnings: string[];
};

function initialWindow(run: ReplayRunSummary): TimelineWindow {
  return {
    run,
    events: [],
    droppedFromFront: 0,
    hasMore: true,
    nextCursor: null,
    bookmarks: [],
    bookmarksTruncated: false,
    warnings: [],
  };
}

function mergePage(window: TimelineWindow, page: ReplayTimeline): TimelineWindow {
  let events = [...window.events, ...page.events];
  let droppedFromFront = window.droppedFromFront;
  if (events.length > REPLAY_TUNABLES.windowSize) {
    const overflow = events.length - REPLAY_TUNABLES.windowSize;
    events = events.slice(overflow);
    droppedFromFront += overflow;
  }
  return {
    ...window,
    events,
    droppedFromFront,
    hasMore: page.has_more,
    nextCursor: page.next_cursor,
    bookmarks: window.bookmarks.length ? window.bookmarks : page.bookmarks,
    bookmarksTruncated: window.bookmarksTruncated || page.bookmarks_truncated,
    warnings: Array.from(new Set([...window.warnings, ...page.warnings])),
  };
}

async function fetchNextPage(
  runId: string,
  window: TimelineWindow,
  signal: AbortSignal,
): Promise<ReplayTimeline> {
  return getReplayTimeline(runId, {
    cursor: window.nextCursor ?? undefined,
    limit: REPLAY_TUNABLES.pageSize,
    signal,
  });
}

export function ReplayScrubberPanel({ ticket }: { ticket: string }) {
  const [runs, setRuns] = useState<ReplayRunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsTruncated, setRunsTruncated] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TimelineWindow | null>(null);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(false);
  const [pageLoading, setPageLoading] = useState(false);
  const [absoluteIndex, setAbsoluteIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<SpeedChoice>(1);
  const [rawEvent, setRawEvent] = useState<{
    rawSeq: number;
    event: ReplayRawEvent;
  } | null>(null);
  const [rawLoading, setRawLoading] = useState(false);
  const [rawError, setRawError] = useState<string | null>(null);
  const loadingRef = useRef<AbortController | null>(null);
  // Round-5 review item 3: prefetch request identity. Each new prefetch
  // bumps ``prefetchRequestId``; the ``finally`` clears ``pageLoading``
  // only when the completing request is still the latest. That way an
  // aborted prefetch superseded by a new one still lets the fresh
  // request drive the loading indicator, and an aborted prefetch with
  // NO successor still clears the indicator (no stale spinner).
  const prefetchRequestRef = useRef(0);

  useEffect(() => {
    let ignore = false;
    const controller = new AbortController();
    setRunsError(null);
    getAgentReplayRuns(ticket, controller.signal)
      .then((body) => {
        if (ignore) return;
        setRuns(body.runs);
        setRunsTruncated(body.runs_truncated);
        setSelectedRunId((current) => current ?? body.runs[0]?.run_id ?? null);
      })
      .catch((err) => {
        if (ignore || controller.signal.aborted) return;
        setRunsError(err instanceof Error ? err.message : "Could not list runs");
        setRuns([]);
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [ticket]);

  // Initial page load whenever the selected run changes.
  useEffect(() => {
    if (!selectedRunId) return;
    let ignore = false;
    const controller = new AbortController();
    loadingRef.current?.abort();
    loadingRef.current = controller;
    setTimeline(null);
    setTimelineError(null);
    setInitialLoading(true);
    setAbsoluteIndex(0);
    setPlaying(false);
    getReplayTimeline(selectedRunId, {
      limit: REPLAY_TUNABLES.pageSize,
      signal: controller.signal,
    })
      .then((page) => {
        if (ignore || controller.signal.aborted) return;
        const window: TimelineWindow = {
          run: page.run,
          events: page.events,
          droppedFromFront: 0,
          hasMore: page.has_more,
          nextCursor: page.next_cursor,
          bookmarks: page.bookmarks,
          bookmarksTruncated: page.bookmarks_truncated,
          warnings: Array.from(new Set(page.warnings)),
        };
        setTimeline(window);
        setInitialLoading(false);
      })
      .catch((err) => {
        if (ignore || controller.signal.aborted) return;
        setTimelineError(
          err instanceof Error ? err.message : "Could not load timeline",
        );
        setInitialLoading(false);
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [selectedRunId]);

  // Demand-driven prefetch: when the cursor is close to the trailing edge
  // of the loaded window AND the server has more, request the next page.
  // Deliberately NOT gated on ``pageLoading`` — if the cursor moves during
  // an in-flight prefetch, the cleanup aborts it and this effect
  // re-fires. Round-3's ``pageLoading`` gate caused the chain to stall
  // after a scrub if the prior abort branch hadn't cleared the flag.
  useEffect(() => {
    if (!timeline || !selectedRunId) return;
    if (!timeline.hasMore || !timeline.nextCursor) return;
    const trailingEdgeAbsolute = timeline.droppedFromFront + timeline.events.length - 1;
    if (absoluteIndex + REPLAY_TUNABLES.prefetchMargin < trailingEdgeAbsolute) return;
    let ignore = false;
    const controller = new AbortController();
    const myRequestId = ++prefetchRequestRef.current;
    setPageLoading(true);
    fetchNextPage(selectedRunId, timeline, controller.signal)
      .then((page) => {
        if (ignore || controller.signal.aborted) return;
        setTimeline((current) => (current ? mergePage(current, page) : current));
      })
      .catch((err) => {
        if (ignore || controller.signal.aborted) return;
        setTimelineError(
          err instanceof Error ? err.message : "Could not load next page",
        );
      })
      .finally(() => {
        // Only clear the loading flag if this is still the LATEST
        // prefetch request. A newer request in flight will drive the
        // indicator on its own completion; if there is no successor,
        // this branch fires and clears the stale spinner.
        if (myRequestId === prefetchRequestRef.current) {
          setPageLoading(false);
        }
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [absoluteIndex, selectedRunId, timeline]);

  // Backward-scrub reset: if the cursor moves before the evicted-edge, we
  // restart pagination from the beginning and page forward until the
  // target is loaded again.
  const rewindTo = useCallback(
    (targetAbsolute: number) => {
      if (!selectedRunId) return;
      const controller = new AbortController();
      loadingRef.current?.abort();
      loadingRef.current = controller;
      setInitialLoading(true);
      setTimeline((current) =>
        current
          ? { ...current, events: [], droppedFromFront: 0, hasMore: true, nextCursor: null }
          : current,
      );
      setPlaying(false);
      (async () => {
        // Round-5 review item 2: evict AFTER EACH PAGE during rewind so
        // the timeline never accumulates the entire run in memory just
        // because the target is late. ``absoluteEdge`` tracks the highest
        // absolute index we've walked past — combined with ``droppedFromFront``
        // it tells the loop when the target sits inside the loaded window.
        let cursor: string | null = null;
        let events: ReplayTimelineEvent[] = [];
        let droppedFromFront = 0;
        let bookmarks: ReplayBookmark[] = [];
        let bookmarksTruncated = false;
        let warnings: string[] = [];
        let run: ReplayRunSummary | null = null;
        let hasMore = true;
        while (hasMore && !controller.signal.aborted) {
          const page = await getReplayTimeline(selectedRunId, {
            cursor: cursor ?? undefined,
            limit: REPLAY_TUNABLES.pageSize,
            signal: controller.signal,
          });
          if (!run) run = page.run;
          events = [...events, ...page.events];
          // Evict eagerly per page — round-4 rewind collected everything
          // then sliced once at the end, which momentarily held the whole
          // run in memory.
          if (events.length > REPLAY_TUNABLES.windowSize) {
            const overflow = events.length - REPLAY_TUNABLES.windowSize;
            events = events.slice(overflow);
            droppedFromFront += overflow;
          }
          if (bookmarks.length === 0) bookmarks = page.bookmarks;
          bookmarksTruncated = bookmarksTruncated || page.bookmarks_truncated;
          warnings = Array.from(new Set([...warnings, ...page.warnings]));
          hasMore = page.has_more;
          cursor = page.next_cursor;
          const absoluteEdge = droppedFromFront + events.length;
          if (absoluteEdge > targetAbsolute + REPLAY_TUNABLES.prefetchMargin) break;
        }
        if (controller.signal.aborted) return;
        setTimeline({
          run: run!,
          events,
          droppedFromFront,
          hasMore,
          nextCursor: cursor,
          bookmarks,
          bookmarksTruncated,
          warnings,
        });
        setAbsoluteIndex(targetAbsolute);
        setInitialLoading(false);
      })().catch((err) => {
        if (controller.signal.aborted) return;
        setTimelineError(err instanceof Error ? err.message : "Could not rewind");
        setInitialLoading(false);
      });
    },
    [selectedRunId],
  );

  // Playback advances one event per real-time gap (scaled by speed).
  useEffect(() => {
    if (!playing || !timeline || timeline.events.length === 0) return;
    const localIndex = absoluteIndex - timeline.droppedFromFront;
    if (localIndex < 0 || localIndex >= timeline.events.length - 1) {
      if (!timeline.hasMore) setPlaying(false);
      return;
    }
    const delay = eventDelay(timeline.events, localIndex, speed);
    const timer = window.setTimeout(() => setAbsoluteIndex((v) => v + 1), delay);
    return () => window.clearTimeout(timer);
  }, [absoluteIndex, playing, speed, timeline]);

  const currentEvent = useMemo(() => {
    if (!timeline) return null;
    const localIndex = absoluteIndex - timeline.droppedFromFront;
    if (localIndex < 0 || localIndex >= timeline.events.length) return null;
    return timeline.events[localIndex];
  }, [absoluteIndex, timeline]);
  const selectedRawSeqRef = useRef<number | null>(null);
  selectedRawSeqRef.current = currentEvent?.raw_seq ?? null;

  useEffect(() => {
    if (!selectedRunId || !currentEvent) {
      setRawEvent(null);
      setRawError(null);
      setRawLoading(false);
      return;
    }
    let ignore = false;
    const controller = new AbortController();
    const rawSeq = currentEvent.raw_seq;
    setRawLoading(true);
    setRawError(null);
    setRawEvent(null);
    const timer = window.setTimeout(() => {
      getReplayRawEvent(selectedRunId, rawSeq, controller.signal)
        .then((result) => {
          if (ignore || controller.signal.aborted || selectedRawSeqRef.current !== rawSeq) return;
          setRawEvent({ rawSeq, event: result });
          setRawLoading(false);
        })
        .catch((err) => {
          if (ignore || controller.signal.aborted || selectedRawSeqRef.current !== rawSeq) return;
          setRawEvent(null);
          setRawError(err instanceof Error ? err.message : "Could not load event");
          setRawLoading(false);
        });
    }, 120);
    return () => {
      ignore = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [currentEvent, selectedRunId]);

  const totalKnown = timeline
    ? Math.max(
        timeline.droppedFromFront + timeline.events.length,
        timeline.run.total_events,
      )
    : 0;

  const bookmarkPositions = useMemo(() => {
    if (!timeline || totalKnown <= 0) return [];
    // Map seq → absolute index via a linear walk over the current window.
    // For bookmarks whose seq isn't in the window we approximate the
    // position by treating seq-1 as the absolute index (seqs are
    // monotonic starting at 1 in supervisor output).
    const positions: {
      bookmark: ReplayBookmark;
      absoluteIndex: number;
      pct: number;
    }[] = [];
    const denom = Math.max(totalKnown - 1, 1);
    for (const bookmark of timeline.bookmarks) {
      const absoluteIndexEstimate = Math.max(0, bookmark.seq - 1);
      const pct = (absoluteIndexEstimate / denom) * 100;
      positions.push({ bookmark, absoluteIndex: absoluteIndexEstimate, pct });
    }
    return positions;
  }, [timeline, totalKnown]);

  const stepBy = useCallback(
    (delta: number) => {
      if (!timeline) return;
      setPlaying(false);
      const target = Math.max(0, Math.min(totalKnown - 1, absoluteIndex + delta));
      if (target < timeline.droppedFromFront) {
        rewindTo(target);
        return;
      }
      setAbsoluteIndex(target);
    },
    [absoluteIndex, rewindTo, timeline, totalKnown],
  );

  const jumpToAbsolute = useCallback(
    (target: number) => {
      if (!timeline) return;
      setPlaying(false);
      if (target < timeline.droppedFromFront) {
        rewindTo(target);
        return;
      }
      setAbsoluteIndex(target);
    },
    [rewindTo, timeline],
  );

  const runOptions = runs ?? [];
  const runsEmpty = runs !== null && runs.length === 0;

  return (
    <div className="replay-panel">
      <div className="replay-toolbar">
        <label className="replay-run-picker">
          <span className="replay-label">run</span>
          <select
            aria-label="Session run"
            className="replay-run-select"
            disabled={runOptions.length === 0}
            value={selectedRunId ?? ""}
            onChange={(event) => setSelectedRunId(event.target.value || null)}
          >
            {runOptions.length === 0 ? <option value="">no runs</option> : null}
            {runOptions.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {formatRunLabel(run)}
              </option>
            ))}
          </select>
        </label>
        {timeline?.run.agent_id ? (
          <span className="replay-meta">
            {timeline.run.agent_id}
            {timeline.run.orch_id ? ` · ${timeline.run.orch_id}` : ""}
            {timeline.run.outcome ? ` · ${timeline.run.outcome}` : ""}
          </span>
        ) : null}
      </div>

      {runsError ? <div className="replay-empty">{runsError}</div> : null}
      {runsEmpty ? (
        <div className="replay-empty">no archived runs for {ticket}</div>
      ) : null}
      {runsTruncated ? (
        <div className="replay-status">older runs beyond this list; truncation cap hit.</div>
      ) : null}

      {timelineError ? (
        <div className="replay-empty">{timelineError}</div>
      ) : null}

      {initialLoading && !timeline ? (
        <div className="replay-empty">loading timeline…</div>
      ) : null}

      {timeline ? (
        <ReplayLoadStatus
          absoluteIndex={absoluteIndex}
          bookmarksTruncated={timeline.bookmarksTruncated}
          droppedFromFront={timeline.droppedFromFront}
          eventsInWindow={timeline.events.length}
          hasMore={timeline.hasMore}
          pageLoading={pageLoading}
          serverWarnings={timeline.warnings}
          totalKnown={totalKnown}
        />
      ) : null}

      {timeline && timeline.events.length > 0 ? (
        <ReplayScrubberBody
          absoluteIndex={absoluteIndex}
          bookmarks={bookmarkPositions}
          currentEvent={currentEvent}
          ticket={ticket}
          onJump={jumpToAbsolute}
          onPlayToggle={() => setPlaying((value) => !value)}
          onSpeedChange={setSpeed}
          onStepBy={stepBy}
          playing={playing}
          rawError={rawError}
          rawEvent={rawEvent}
          rawLoading={rawLoading}
          speed={speed}
          totalKnown={totalKnown}
          window={timeline}
        />
      ) : null}
    </div>
  );
}

function ReplayLoadStatus({
  absoluteIndex,
  bookmarksTruncated,
  droppedFromFront,
  eventsInWindow,
  hasMore,
  pageLoading,
  serverWarnings,
  totalKnown,
}: {
  absoluteIndex: number;
  bookmarksTruncated: boolean;
  droppedFromFront: number;
  eventsInWindow: number;
  hasMore: boolean;
  pageLoading: boolean;
  serverWarnings: string[];
  totalKnown: number;
}) {
  const messages: string[] = [];
  if (pageLoading) {
    messages.push(`loading next page…`);
  }
  if (droppedFromFront > 0) {
    messages.push(
      `sliding window: showing events ${droppedFromFront + 1}–${droppedFromFront + eventsInWindow} (of ~${totalKnown}). Earlier events evicted; scrub back to reload.`,
    );
  }
  if (bookmarksTruncated) {
    messages.push("bookmark list truncated — more bookmarks exist beyond this view.");
  }
  const dedupedWarnings = Array.from(new Set(serverWarnings));
  for (const warning of dedupedWarnings) {
    messages.push(warning);
  }
  if (messages.length === 0) return null;
  return (
    <div aria-live="polite" className="replay-status" role="status">
      {messages.map((message, i) => (
        <p key={i}>{message}</p>
      ))}
    </div>
  );
}

function ReplayScrubberBody({
  absoluteIndex,
  bookmarks,
  currentEvent,
  ticket,
  onJump,
  onPlayToggle,
  onSpeedChange,
  onStepBy,
  playing,
  rawError,
  rawEvent,
  rawLoading,
  speed,
  totalKnown,
  window: replayWindow,
}: {
  absoluteIndex: number;
  bookmarks: { bookmark: ReplayBookmark; absoluteIndex: number; pct: number }[];
  currentEvent: ReplayTimelineEvent | null;
  ticket: string;
  onJump: (absoluteIndex: number) => void;
  onPlayToggle: () => void;
  onSpeedChange: (speed: SpeedChoice) => void;
  onStepBy: (delta: number) => void;
  playing: boolean;
  rawError: string | null;
  rawEvent: { rawSeq: number; event: ReplayRawEvent } | null;
  rawLoading: boolean;
  speed: SpeedChoice;
  totalKnown: number;
  window: TimelineWindow;
}) {
  const rawRef = useRef<HTMLPreElement | null>(null);
  const selectedRawEvent = currentEvent && rawEvent?.rawSeq === currentEvent.raw_seq
    ? rawEvent.event
    : null;
  const presentation = currentEvent
    ? providerEventPresentation(currentEvent, selectedRawEvent)
    : null;

  useEffect(() => {
    if (rawRef.current) rawRef.current.scrollTop = 0;
  }, [selectedRawEvent]);

  const rawJson = useMemo(() => {
    if (rawError) return rawError;
    if (rawLoading) return "loading…";
    if (!selectedRawEvent) return "";
    try {
      return JSON.stringify(selectedRawEvent.raw, null, 2);
    } catch {
      return "unrenderable payload";
    }
  }, [rawError, rawLoading, selectedRawEvent]);

  const sliderMax = Math.max(totalKnown - 1, 0);
  const timePct = sliderMax === 0 ? 0 : (absoluteIndex / sliderMax) * 100;
  const beyondWindow = currentEvent === null;

  return (
    <>
      <div className="replay-scrubber">
        <div className="replay-scrubber-row">
          <button
            aria-label="Previous event"
            className="replay-step"
            disabled={absoluteIndex <= 0}
            type="button"
            onClick={() => onStepBy(-1)}
          >
            <SkipBack size={13} />
          </button>
          <button
            aria-label={playing ? "Pause replay" : "Play replay"}
            aria-pressed={playing}
            className="replay-play"
            type="button"
            onClick={onPlayToggle}
          >
            {playing ? <Pause size={13} /> : <Play size={13} />}
            <span>{playing ? "pause" : "play"}</span>
          </button>
          <button
            aria-label="Next event"
            className="replay-step"
            disabled={absoluteIndex >= sliderMax}
            type="button"
            onClick={() => onStepBy(1)}
          >
            <SkipForward size={13} />
          </button>
          <div aria-label="Replay progress" className="replay-track" role="group">
            <input
              aria-label="Event cursor"
              className="replay-slider"
              max={sliderMax}
              min={0}
              type="range"
              value={absoluteIndex}
              onChange={(event) => onJump(Number(event.target.value))}
            />
            <div aria-hidden className="replay-track-progress" style={{ width: `${timePct}%` }} />
            {bookmarks.map(({ bookmark, absoluteIndex: bookmarkAbs, pct }) => (
              <button
                key={`${bookmark.seq}-${bookmark.kind}`}
                aria-label={`Jump to ${bookmark.kind} at seq ${bookmark.seq}`}
                className={`replay-bookmark is-${bookmark.kind}`}
                data-active={bookmarkAbs === absoluteIndex}
                style={{ left: `${pct}%` }}
                title={bookmarkTitle(bookmark)}
                type="button"
                onClick={() => onJump(bookmarkAbs)}
              >
                {bookmarkIcon(bookmark.kind)}
              </button>
            ))}
          </div>
          <label className="replay-speed">
            <span className="replay-label">speed</span>
            <select
              aria-label="Playback speed"
              value={String(speed)}
              onChange={(event) => {
                const raw = event.target.value;
                onSpeedChange(
                  raw === MAX_SPEED_LABEL ? MAX_SPEED_LABEL : (Number(raw) as SpeedChoice),
                );
              }}
            >
              {SPEED_OPTIONS.map((option) => (
                <option key={option} value={String(option)}>
                  {option}x
                </option>
              ))}
              <option value={MAX_SPEED_LABEL}>max</option>
            </select>
          </label>
        </div>
        <div className="replay-frame-info">
          <span>
            {absoluteIndex + 1} / {Math.max(totalKnown, absoluteIndex + 1)}
          </span>
          <span className="replay-frame-clock">
            {formatClockTime(currentEvent?.ts ?? null)}
          </span>
          {currentEvent ? (
            <span className="replay-frame-kind">
              {EVENT_KIND_LABELS[currentEvent.kind] ?? "event"}
            </span>
          ) : null}
          {beyondWindow && replayWindow.hasMore ? (
            <span className="replay-frame-kind">loading window…</span>
          ) : null}
        </div>
      </div>

      {currentEvent ? (
        <article className="replay-event">
          <div className="replay-event-summary">
            {presentation ? (
              <ReplayPresentationBlocks blocks={presentation.blocks} event={currentEvent} ticket={ticket} />
            ) : null}
            {rawLoading && !selectedRawEvent ? (
              <span aria-live="polite" className="replay-event-loading">loading event…</span>
            ) : null}
          </div>
          <div className="replay-event-status" aria-label="Event status">
            <span>{plainEnum(currentEvent.disposition, DISPOSITION_LABELS)}</span>
            {currentEvent.lifecycle_state ? (
              <span>{plainEnum(currentEvent.lifecycle_state, LIFECYCLE_LABELS)}</span>
            ) : null}
            {currentEvent.bookmark ? (
              <span>{plainEnum(currentEvent.bookmark, BOOKMARK_LABELS)}</span>
            ) : null}
          </div>
          <details key={currentEvent.seq} className="replay-event-details">
            <summary>Event details</summary>
            <div className="replay-event-details-body">
              <div className="replay-event-details-meta">
                <span>seq {currentEvent.seq}</span>
                <span>kind {currentEvent.kind}</span>
                <span>disposition {currentEvent.disposition}</span>
                {currentEvent.lifecycle_state ? (
                  <span>lifecycle {currentEvent.lifecycle_state}</span>
                ) : null}
                {currentEvent.bookmark ? <span>bookmark {currentEvent.bookmark}</span> : null}
              </div>
              <div className="replay-event-raw-wrap">
                <pre ref={rawRef} className="replay-event-raw">
                  {rawJson}
                </pre>
                {selectedRawEvent && !rawLoading && !rawError ? (
                  <CopyPill
                    className="replay-event-copy"
                    getText={() => rawJson}
                    label="copy JSON"
                  />
                ) : null}
              </div>
            </div>
          </details>
        </article>
      ) : beyondWindow ? (
        <div className="replay-empty">
          event {absoluteIndex + 1} is outside the loaded window
          {replayWindow.hasMore ? " — loading more…" : ""}
        </div>
      ) : null}
    </>
  );
}
