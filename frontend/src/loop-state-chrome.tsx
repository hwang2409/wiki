import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, Power, Repeat } from "lucide-react";
import {
  getAgentWorkgraph,
  getAutopilotStatus,
  setAutopilotEnabled,
  type AutopilotState,
  type LoopState,
  type LoopStateHistoryEntry,
} from "./api";

type Props = {
  ticket: string;
  tick: number;
};

function shortTime(value: string | null | undefined): string {
  if (!value) return "";
  return value.slice(11, 16) || value;
}

function relativeMinutes(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return "";
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  const minutes = Math.max(0, Math.round((end - start) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rem = minutes % 60;
  return rem === 0 ? `${hours}h` : `${hours}h${rem.toString().padStart(2, "0")}`;
}

function verdictClass(state: string | null | undefined): string {
  if (!state) return "";
  if (state === "MERGE-READY") return "is-merge-ready";
  if (state === "NOT-MERGE-READY") return "is-not-merge-ready";
  if (state === "NO-GO") return "is-no-go";
  return "is-insufficient";
}

function verdictLabel(state: string | null | undefined): string {
  if (!state) return "—";
  return state.toLowerCase();
}

function actionTime(atNs: number): string {
  if (!Number.isFinite(atNs)) return "";
  return new Date(atNs / 1_000_000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function actionText(action: AutopilotState["actions"][number], key: string): string | null {
  const value = action[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function ActionRow({ action }: { action: AutopilotState["actions"][number] }) {
  const state = actionText(action, "state");
  const findingCount = action["finding_count"];
  const preview = actionText(action, "preview");
  const halted = actionText(action, "halted");
  const sha =
    actionText(action, "head_sha") ??
    actionText(action, "source_sha") ??
    actionText(action, "expected_sha");
  const detailParts = [
    actionText(action, "reviewer"),
    actionText(action, "target"),
    actionText(action, "pr"),
    sha ? `sha ${sha}` : null,
  ].filter((value): value is string => Boolean(value));
  return (
    <div className="loop-autopilot-log-row">
      <div className="loop-autopilot-log-main">
        <div className="loop-autopilot-log-head">
          <span>{action.action}</span>
          <span className="loop-autopilot-log-time tabular-nums">{actionTime(action.at_ns)}</span>
        </div>
        {state ? (
          <div className="loop-autopilot-log-summary">
            verdict {state}
            {typeof findingCount === "number" ? ` · ${findingCount} findings` : ""}
          </div>
        ) : null}
        {preview ? <div className="loop-autopilot-log-preview">steer: {preview}</div> : null}
        {detailParts.length > 0 ? (
          <div className="loop-autopilot-log-details">{detailParts.join(" · ")}</div>
        ) : null}
        {halted ? (
          <div className="loop-autopilot-log-halted">halted: {halted}</div>
        ) : null}
      </div>
    </div>
  );
}

function HistoryRow({ entry }: { entry: LoopStateHistoryEntry }) {
  const routedDelta = relativeMinutes(entry.verdict_at, entry.routed_at);
  const archivedDelta = relativeMinutes(entry.spawned_at, entry.archived_at);
  return (
    <li className="loop-history-row">
      <span className="loop-history-round tabular-nums">r{entry.round}</span>
      <div className="loop-history-body">
        <div className="loop-history-line">
          <span className={`loop-history-verdict ${verdictClass(entry.verdict_state)}`}>
            {verdictLabel(entry.verdict_state)}
          </span>
          {entry.reviewer ? (
            <span className="loop-history-reviewer">{entry.reviewer}</span>
          ) : null}
          {entry.spawned_at ? (
            <span className="loop-history-time tabular-nums">
              spawned {shortTime(entry.spawned_at)}
            </span>
          ) : null}
          {entry.verdict_at ? (
            <span className="loop-history-time tabular-nums">
              verdict {shortTime(entry.verdict_at)}
            </span>
          ) : null}
          {entry.routed_at ? (
            <span className="loop-history-time tabular-nums">
              routed +{routedDelta}
            </span>
          ) : entry.verdict_state && entry.verdict_state !== "MERGE-READY" ? (
            <span className="loop-history-time is-unrouted">not routed</span>
          ) : null}
          {entry.archived_at ? (
            <span className="loop-history-time tabular-nums">
              archived +{archivedDelta}
            </span>
          ) : null}
        </div>
        {entry.top_finding ? (
          <div className="loop-history-finding">{entry.top_finding}</div>
        ) : null}
      </div>
    </li>
  );
}

export function LoopStateChrome({ ticket, tick }: Props) {
  const [state, setState] = useState<LoopState | null>(null);
  const [autopilot, setAutopilot] = useState<AutopilotState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const detailRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    setState(null);
    setAutopilot(null);
    setLoaded(false);
    setOpen(false);
  }, [ticket]);

  useEffect(() => {
    let cancelled = false;
    getAgentWorkgraph(ticket)
      .then((data) => {
        if (cancelled) return;
        setState(data.loop_state ?? null);
        setLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setState(null);
        setAutopilot(null);
        setLoaded(true);
      });
    getAutopilotStatus(ticket)
      .then((data) => {
        if (cancelled) return;
        setAutopilot(typeof data.enabled === "boolean" ? data : null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [ticket, tick]);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!(event.target instanceof Node)) return;
      // Trigger AND detail belong to the same interactive surface — a
      // pointer sequence on the trigger (mousedown -> click) must resolve
      // to a clean toggle. Excluding the trigger from the outside-click
      // handler prevents mousedown-close + click-reopen fighting each
      // other and leaving the panel stuck open.
      if (detailRef.current && detailRef.current.contains(event.target)) return;
      if (triggerRef.current && triggerRef.current.contains(event.target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const chips = useMemo(() => {
    if (!state) return null;
    const capReached = state.round >= state.cap;
    const roundLabel = `round ${state.round} of ${state.cap}`;
    return (
      <>
        <span
          className={`loop-chrome-chip loop-chrome-round is-${state.danger}${capReached ? " is-cap" : ""}`}
          data-testid="loop-chrome-round"
        >
          <span className="loop-chrome-glyph tabular-nums">
            <span className="loop-chrome-round-num">{state.round}</span>
            <span className="loop-chrome-round-slash">/</span>
            <span className="loop-chrome-round-cap">{state.cap}</span>
          </span>
          <span className="loop-chrome-label">{roundLabel}</span>
        </span>
        {state.unrouted_verdict_count > 0 ? (
          <span
            className="loop-chrome-chip loop-chrome-unrouted"
            data-testid="loop-chrome-unrouted"
          >
            <AlertTriangle aria-hidden size={12} />
            <span className="tabular-nums">{state.unrouted_verdict_count}</span>
            <span>
              {state.unrouted_verdict_count === 1 ? "unrouted finding" : "unrouted findings"}
            </span>
          </span>
        ) : null}
        {state.plateau_length >= 2 ? (
          <span
            className="loop-chrome-chip loop-chrome-plateau"
            data-testid="loop-chrome-plateau"
          >
            <Repeat aria-hidden size={12} />
            <span>same finding</span>
            <span className="tabular-nums">{state.plateau_length}</span>
            <span>rounds</span>
          </span>
        ) : null}
      </>
    );
  }, [state]);

  if (!loaded) return null;
  if (!state && !autopilot) {
    return null;
  }

  const hasLoopHistory =
    state && (state.round > 0 || state.unrouted_verdict_count > 0);
  const hasAutopilotLog = Boolean(autopilot?.actions?.length);
  const hasDetails = Boolean(hasLoopHistory || hasAutopilotLog);

  return (
    <div className="loop-chrome" data-danger={state?.danger ?? "normal"}>
      <div className="loop-chrome-controls">
        {hasDetails ? (
          <button
            aria-expanded={open}
            aria-label={
              open ? "Hide merge-ready loop history" : "Show merge-ready loop history"
            }
            className={`loop-chrome-trigger${open ? " is-open" : ""}`}
            ref={triggerRef}
            type="button"
            onClick={() => setOpen((current) => !current)}
          >
            {state ? chips : <span className="loop-chrome-label">autopilot log</span>}
            <ChevronDown
              aria-hidden
              className={`loop-chrome-caret${open ? " is-open" : ""}`}
              size={12}
            />
          </button>
        ) : null}
        {autopilot ? (
          <button
            aria-pressed={autopilot.enabled}
            className={`loop-autopilot-toggle${autopilot.enabled ? " is-on" : ""}`}
            type="button"
            onClick={() => {
              void setAutopilotEnabled(ticket, !autopilot.enabled)
                .then(setAutopilot)
                .catch(() => undefined);
            }}
          >
            <Power aria-hidden size={12} />
            autopilot {autopilot.enabled ? "on" : "off"}
          </button>
        ) : null}
      </div>
      {open && (state || hasAutopilotLog) ? (
        <div className="loop-chrome-detail" ref={detailRef} role="dialog">
          <header className="loop-chrome-detail-head">
            <span className="loop-chrome-detail-title">merge-ready loop</span>
            {state ? (
              <span className="loop-chrome-detail-sub tabular-nums">
                {state.round}/{state.cap} rounds
                {state.plateau_length >= 2
                  ? ` · plateau ${state.plateau_length}`
                  : ""}
                {state.unrouted_verdict_count > 0
                  ? ` · ${state.unrouted_verdict_count} unrouted`
                  : ""}
              </span>
            ) : null}
          </header>
          {state && (state.latest_verdict_finding ?? state.latest_verdict?.top_finding?.title) ? (
            <div className="loop-chrome-latest">
              <span className="loop-chrome-latest-label">latest finding</span>
              <span className="loop-chrome-latest-title">
                {state.latest_verdict_finding ??
                  state.latest_verdict?.top_finding?.title ??
                  ""}
              </span>
            </div>
          ) : null}
          {state && state.history.length > 0 ? (
            <ol className="loop-history">
              {state.history.map((entry) => (
                <HistoryRow entry={entry} key={`${entry.round}-${entry.reviewer ?? ""}`} />
              ))}
            </ol>
          ) : state ? (
            <div className="loop-chrome-empty">no rounds recorded yet</div>
          ) : null}
          {autopilot?.actions?.length ? (
            <div className="loop-autopilot-log">
              <div className="loop-chrome-latest-label">autopilot log</div>
              {autopilot.actions
                .slice(-5)
                .reverse()
                .map((action) => (
                  <ActionRow action={action} key={`${action.at_ns}-${action.action}`} />
                ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
