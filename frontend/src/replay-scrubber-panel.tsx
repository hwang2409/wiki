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
} from "./api";

const SPEED_OPTIONS = [0.5, 1, 2, 5, 10] as const;
const MAX_SPEED_LABEL = "max";
type SpeedChoice = (typeof SPEED_OPTIONS)[number] | typeof MAX_SPEED_LABEL;

const MAX_ADVANCE_DELAY_MS = 10_000;
const MIN_ADVANCE_DELAY_MS = 40;
const DEFAULT_PAGE_SIZE = 500;
// Runaway guard — a well-behaved server always terminates ``has_more``,
// but we cap pagination at 200 pages (100k events at DEFAULT_PAGE_SIZE) so a
// corrupt cursor cycle can't loop forever. If we hit this, the UI shows an
// explicit "more events beyond this window" notice.
const MAX_TIMELINE_PAGES = 200;

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

type TimelineLoadUpdate = {
  timeline: ReplayTimeline;
  pagesLoaded: number;
  done: boolean;
  hitPageGuard: boolean;
};

/**
 * Page through every server window until the cursor drains. Reports each
 * intermediate state through ``onProgress`` so a long run shows a growing
 * event count instead of a spinner that hides the fact loading is still
 * happening. The MAX_TIMELINE_PAGES guard exists only as a runaway backstop;
 * if we ever hit it the caller flips a "more events beyond this window"
 * banner — the round-1 code silently truncated at 5000 events which is what
 * the review flagged.
 */
async function loadFullTimeline(
  runId: string,
  signal: AbortSignal,
  onProgress: (update: TimelineLoadUpdate) => void,
): Promise<void> {
  let timeline = await getReplayTimeline(runId, {
    limit: DEFAULT_PAGE_SIZE,
    signal,
  });
  let pages = 1;
  onProgress({
    timeline,
    pagesLoaded: pages,
    done: !timeline.has_more,
    hitPageGuard: false,
  });
  let cursor: string | null = timeline.next_cursor;
  while (timeline.has_more && cursor && !signal.aborted) {
    if (pages >= MAX_TIMELINE_PAGES) {
      onProgress({
        timeline,
        pagesLoaded: pages,
        done: false,
        hitPageGuard: true,
      });
      return;
    }
    const page = await getReplayTimeline(runId, {
      cursor,
      limit: DEFAULT_PAGE_SIZE,
      signal,
    });
    pages += 1;
    // Merge warnings even on an empty final page — a truncated tail or
    // scan cap warning can appear on the last read after we've stopped
    // accumulating new events.
    const mergedWarnings = [...timeline.warnings, ...page.warnings];
    timeline = {
      ...timeline,
      events: [...timeline.events, ...page.events],
      next_cursor: page.next_cursor,
      has_more: page.has_more,
      bookmarks_truncated:
        timeline.bookmarks_truncated || page.bookmarks_truncated,
      warnings: mergedWarnings,
    };
    cursor = page.next_cursor;
    onProgress({
      timeline,
      pagesLoaded: pages,
      done: !timeline.has_more,
      hitPageGuard: false,
    });
    if (!page.has_more) return;
  }
}

export function ReplayScrubberPanel({ ticket }: { ticket: string }) {
  const [runs, setRuns] = useState<ReplayRunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<ReplayTimeline | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [pagesLoaded, setPagesLoaded] = useState(0);
  const [pageGuardHit, setPageGuardHit] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<SpeedChoice>(1);
  const [rawEvent, setRawEvent] = useState<ReplayRawEvent | null>(null);
  const [rawLoading, setRawLoading] = useState(false);
  const [rawError, setRawError] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    const controller = new AbortController();
    setRunsError(null);
    getAgentReplayRuns(ticket, controller.signal)
      .then((body) => {
        if (ignore) return;
        setRuns(body.runs);
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

  useEffect(() => {
    if (!selectedRunId) return;
    let ignore = false;
    const controller = new AbortController();
    setTimeline(null);
    setTimelineError(null);
    setTimelineLoading(true);
    setPagesLoaded(0);
    setPageGuardHit(false);
    setCursor(0);
    setPlaying(false);
    loadFullTimeline(selectedRunId, controller.signal, (update) => {
      if (ignore || controller.signal.aborted) return;
      setTimeline(update.timeline);
      setPagesLoaded(update.pagesLoaded);
      setPageGuardHit(update.hitPageGuard);
      if (update.done || update.hitPageGuard) {
        setTimelineLoading(false);
      }
    })
      .catch((err) => {
        if (ignore || controller.signal.aborted) return;
        setTimeline(null);
        setTimelineError(
          err instanceof Error ? err.message : "Could not load timeline"
        );
        setTimelineLoading(false);
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [selectedRunId]);

  useEffect(() => {
    if (!playing || !timeline || timeline.events.length === 0) return;
    if (cursor >= timeline.events.length - 1) {
      setPlaying(false);
      return;
    }
    const delay = eventDelay(timeline.events, cursor, speed);
    const timer = window.setTimeout(() => setCursor((v) => v + 1), delay);
    return () => window.clearTimeout(timer);
  }, [cursor, playing, speed, timeline]);

  const currentEvent = timeline?.events[cursor] ?? null;

  useEffect(() => {
    if (!selectedRunId || !currentEvent) {
      setRawEvent(null);
      setRawError(null);
      setRawLoading(false);
      return;
    }
    let ignore = false;
    const controller = new AbortController();
    setRawLoading(true);
    setRawError(null);
    const timer = window.setTimeout(() => {
      getReplayRawEvent(selectedRunId, currentEvent.raw_seq, controller.signal)
        .then((result) => {
          if (ignore || controller.signal.aborted) return;
          setRawEvent(result);
          setRawLoading(false);
        })
        .catch((err) => {
          if (ignore || controller.signal.aborted) return;
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

  const bookmarkPositions = useMemo(() => {
    if (!timeline || timeline.events.length === 0) return [];
    const total = timeline.events.length;
    const seqToIndex = new Map<number, number>();
    timeline.events.forEach((event, index) => {
      seqToIndex.set(event.seq, index);
    });
    return timeline.bookmarks
      .map((bookmark) => {
        const index = seqToIndex.get(bookmark.seq);
        if (index === undefined) return null;
        const pct = total <= 1 ? 0 : (index / (total - 1)) * 100;
        return { bookmark, index, pct };
      })
      .filter((value): value is { bookmark: ReplayBookmark; index: number; pct: number } =>
        value !== null,
      );
  }, [timeline]);

  const stepBy = useCallback(
    (delta: number) => {
      if (!timeline) return;
      setPlaying(false);
      setCursor((v) =>
        Math.max(0, Math.min(timeline.events.length - 1, v + delta))
      );
    },
    [timeline]
  );

  const jumpToBookmark = useCallback(
    (index: number) => {
      setPlaying(false);
      setCursor(index);
    },
    []
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

      {timelineError ? (
        <div className="replay-empty">{timelineError}</div>
      ) : null}

      {timelineLoading && !timeline ? (
        <div className="replay-empty">loading timeline…</div>
      ) : null}

      {timeline ? (
        <ReplayLoadStatus
          bookmarksTruncated={timeline.bookmarks_truncated}
          events={timeline.events.length}
          loading={timelineLoading}
          pageGuardHit={pageGuardHit}
          pagesLoaded={pagesLoaded}
          serverWarnings={timeline.warnings}
          totalHint={timeline.run.total_events}
        />
      ) : null}

      {timeline && timeline.events.length > 0 ? (
        <ReplayScrubberBody
          bookmarks={bookmarkPositions}
          cursor={cursor}
          currentEvent={currentEvent}
          onCursorChange={(value) => {
            setPlaying(false);
            setCursor(value);
          }}
          onJumpToBookmark={jumpToBookmark}
          onPlayToggle={() => setPlaying((value) => !value)}
          onSpeedChange={setSpeed}
          onStepBy={stepBy}
          playing={playing}
          rawError={rawError}
          rawEvent={rawEvent}
          rawLoading={rawLoading}
          speed={speed}
          timeline={timeline}
        />
      ) : null}
    </div>
  );
}

function ReplayLoadStatus({
  bookmarksTruncated,
  events,
  loading,
  pageGuardHit,
  pagesLoaded,
  serverWarnings,
  totalHint,
}: {
  bookmarksTruncated: boolean;
  events: number;
  loading: boolean;
  pageGuardHit: boolean;
  pagesLoaded: number;
  serverWarnings: string[];
  totalHint: number;
}) {
  const messages: string[] = [];
  if (loading) {
    if (totalHint > 0) {
      messages.push(`loaded ${events} of ~${totalHint} events (page ${pagesLoaded})…`);
    } else {
      messages.push(`loaded ${events} events (page ${pagesLoaded})…`);
    }
  }
  if (pageGuardHit) {
    messages.push(
      `stopped after ${pagesLoaded} pages; more events exist beyond this window.`
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
    <div
      aria-live="polite"
      className={`replay-status${pageGuardHit ? " is-truncated" : ""}`}
      role="status"
    >
      {messages.map((message, i) => (
        <p key={i}>{message}</p>
      ))}
    </div>
  );
}

function ReplayScrubberBody({
  bookmarks,
  cursor,
  currentEvent,
  onCursorChange,
  onJumpToBookmark,
  onPlayToggle,
  onSpeedChange,
  onStepBy,
  playing,
  rawError,
  rawEvent,
  rawLoading,
  speed,
  timeline,
}: {
  bookmarks: { bookmark: ReplayBookmark; index: number; pct: number }[];
  cursor: number;
  currentEvent: ReplayTimelineEvent | null;
  onCursorChange: (value: number) => void;
  onJumpToBookmark: (index: number) => void;
  onPlayToggle: () => void;
  onSpeedChange: (speed: SpeedChoice) => void;
  onStepBy: (delta: number) => void;
  playing: boolean;
  rawError: string | null;
  rawEvent: ReplayRawEvent | null;
  rawLoading: boolean;
  speed: SpeedChoice;
  timeline: ReplayTimeline;
}) {
  const total = timeline.events.length;
  const rawRef = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    if (rawRef.current) rawRef.current.scrollTop = 0;
  }, [rawEvent]);

  const rawJson = useMemo(() => {
    if (rawError) return rawError;
    if (rawLoading) return "loading…";
    if (!rawEvent) return "";
    try {
      return JSON.stringify(rawEvent.raw, null, 2);
    } catch {
      return "unrenderable payload";
    }
  }, [rawError, rawEvent, rawLoading]);

  const timePct = total <= 1 ? 0 : (cursor / (total - 1)) * 100;

  return (
    <>
      <div className="replay-scrubber">
        <div className="replay-scrubber-row">
          <button
            aria-label="Previous event"
            className="replay-step"
            disabled={cursor <= 0}
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
            disabled={cursor >= total - 1}
            type="button"
            onClick={() => onStepBy(1)}
          >
            <SkipForward size={13} />
          </button>
          <div
            aria-label="Replay progress"
            className="replay-track"
            role="group"
          >
            <input
              aria-label="Event cursor"
              className="replay-slider"
              max={total - 1}
              min={0}
              type="range"
              value={cursor}
              onChange={(event) => onCursorChange(Number(event.target.value))}
            />
            <div aria-hidden className="replay-track-progress" style={{ width: `${timePct}%` }} />
            {bookmarks.map(({ bookmark, index, pct }) => (
              <button
                key={`${bookmark.seq}-${bookmark.kind}`}
                aria-label={`Jump to ${bookmark.kind} at seq ${bookmark.seq}`}
                className={`replay-bookmark is-${bookmark.kind}`}
                data-active={index === cursor}
                style={{ left: `${pct}%` }}
                title={bookmarkTitle(bookmark)}
                type="button"
                onClick={() => onJumpToBookmark(index)}
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
                  raw === MAX_SPEED_LABEL ? MAX_SPEED_LABEL : (Number(raw) as SpeedChoice)
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
            {cursor + 1} / {total}
          </span>
          <span className="replay-frame-clock">{formatClockTime(currentEvent?.ts ?? null)}</span>
          {currentEvent ? (
            <span className="replay-frame-kind">{currentEvent.kind}</span>
          ) : null}
        </div>
      </div>

      {currentEvent ? (
        <article className="replay-event">
          <header className="replay-event-head">
            <span className={`replay-disposition is-${currentEvent.disposition}`}>
              {currentEvent.disposition}
            </span>
            <span className="replay-event-seq">seq {currentEvent.seq}</span>
            {currentEvent.lifecycle_state ? (
              <span className="replay-event-lifecycle">
                {currentEvent.lifecycle_state}
              </span>
            ) : null}
            {currentEvent.bookmark ? (
              <span className={`replay-event-bookmark is-${currentEvent.bookmark}`}>
                {currentEvent.bookmark}
              </span>
            ) : null}
          </header>
          <p className="replay-event-summary">{currentEvent.summary}</p>
          <pre ref={rawRef} className="replay-event-raw">
            {rawJson}
          </pre>
        </article>
      ) : null}
    </>
  );
}
