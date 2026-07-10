import {
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ComponentProps, CSSProperties, RefObject } from "react";
import {
  AlertTriangle,
  Bell,
  Bot,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleDashed,
  CircleSlash,
  Eye,
  FileText,
  GitCommit,
  GitPullRequest,
  Hourglass,
  ListTodo,
  Lock,
  MessageCircleQuestion,
  Pencil,
  Radio,
  ScrollText,
  Search,
  SendHorizontal,
  Server,
  SlashSquare,
  Tag,
  Terminal,
  Wrench,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { MarkdownPre } from "./markdown";
import { ShikiCode } from "./shiki";
import {
  cancelAgentModelChange,
  cancelQueuedMessage,
  getAgentModels,
  getSkills,
  respondToAgentRequest,
  sendAgentMessage,
  setAgentModel,
  uploadImage,
} from "./api";
import type {
  AgentModelOption,
  ProviderEventInspector,
  ProviderPendingRequest,
  QueuedMessage,
  SessionEvent,
  SessionPr,
  SkillInfo,
  SubagentInfo,
} from "./api";
import { renderAnsi } from "./ansi";
import { externalLinkProps } from "./external-links";
import {
  GhPreviewCard,
  containsGitHubPreviewUrl,
  isGitHubPreviewUrl,
  splitGitHubPreviewSegments,
} from "./github-preview";
import { LoadingPlaceholder } from "./loading";
import { createStateKeyWriteBarrier, deletePaneStateEntries } from "./pane-state-cache";
import { Timestamp } from "./timestamp";
import {
  invalidateTranscript,
  replaceTranscriptDesiredModel,
  replaceTranscriptQueue,
  useTranscriptSession,
  type TranscriptSession,
} from "./transcript-store";

const POLL_MS = 2500;
const VIRTUAL_ROW_GAP = 14;
const VIRTUAL_MIN_OVERSCAN = 3600;
const VIRTUAL_OVERSCAN_MULTIPLIER = 5;
const VIRTUAL_DEFAULT_VIEWPORT = 720;
const MIN_ROW_HEIGHT = 24;
const COMPOSER_MIN_HEIGHT = 44;

let skillsCache: SkillInfo[] | null = null;
function useSkills(): SkillInfo[] {
  const [skills, setSkills] = useState<SkillInfo[]>(skillsCache ?? []);
  useEffect(() => {
    if (skillsCache) return;
    getSkills()
      .then((result) => {
        skillsCache = result.skills;
        setSkills(result.skills);
      })
      .catch(() => {});
  }, []);
  return skills;
}

// Mirror-div caret measurement — for the overlay block cursor on blank lines,
// where selecting the newline renders zero-width.
function measureCaret(el: HTMLTextAreaElement, at: number): { top: number; left: number } {
  const mirror = document.createElement("div");
  const style = getComputedStyle(el);
  for (const prop of [
    "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "boxSizing",
  ] as const) {
    mirror.style[prop] = style[prop];
  }
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.width = `${el.clientWidth}px`;
  const prefix = el.value.slice(0, at);
  // A marker directly after a trailing \n measures on the PREVIOUS line — a
  // zero-width space forces the line break to materialize first.
  mirror.textContent = prefix.endsWith("\n") || prefix === "" ? prefix + "\u200b" : prefix;
  const marker = document.createElement("span");
  marker.textContent = "\u200b";
  mirror.appendChild(marker);
  document.body.appendChild(mirror);
  const top = marker.offsetTop - el.scrollTop;
  const left = marker.offsetLeft;
  document.body.removeChild(mirror);
  return { top, left };
}

function createTextareaMeasure(): HTMLTextAreaElement {
  const mirror = document.createElement("textarea");
  mirror.setAttribute("aria-hidden", "true");
  mirror.tabIndex = -1;
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.pointerEvents = "none";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.style.height = "0";
  mirror.style.minHeight = "0";
  mirror.style.maxHeight = "none";
  mirror.style.overflow = "hidden";
  mirror.style.contain = "strict";
  document.body.appendChild(mirror);
  return mirror;
}

function measureTextareaHeight(source: HTMLTextAreaElement, mirror: HTMLTextAreaElement): number {
  const style = getComputedStyle(source);
  for (const prop of [
    "boxSizing",
    "fontFamily",
    "fontSize",
    "fontStyle",
    "fontWeight",
    "letterSpacing",
    "lineHeight",
    "paddingTop",
    "paddingRight",
    "paddingBottom",
    "paddingLeft",
    "borderTopWidth",
    "borderRightWidth",
    "borderBottomWidth",
    "borderLeftWidth",
    "textIndent",
    "textTransform",
    "whiteSpace",
    "wordBreak",
    "overflowWrap",
  ] as const) {
    mirror.style[prop] = style[prop];
  }
  mirror.style.width = `${Math.max(source.clientWidth, Math.ceil(source.getBoundingClientRect().width))}px`;
  mirror.value = source.value
    ? source.value.endsWith("\n")
      ? `${source.value} `
      : source.value
    : " ";
  return Math.max(mirror.scrollHeight, COMPOSER_MIN_HEIGHT);
}

// "/par" or "$par" at the caret, at start-of-word → autocomplete trigger
const SKILL_TRIGGER = /(?:^|\s)([/$])([\w-]*)$/;

export function usePollTick(refreshTick: number): number {
  const [pollTick, setPollTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setPollTick((tick) => tick + 1), POLL_MS);
    return () => window.clearInterval(id);
  }, []);
  return pollTick + refreshTick;
}

function useElementVisible(ref: RefObject<HTMLElement | null>): boolean {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    const compute = () => {
      const rect = node.getBoundingClientRect();
      const inViewport =
        rect.width > 0 &&
        rect.height > 0 &&
        rect.bottom > 0 &&
        rect.right > 0 &&
        rect.top < window.innerHeight &&
        rect.left < window.innerWidth;
      setVisible(document.visibilityState === "visible" && inViewport);
    };

    const observer = new IntersectionObserver(() => compute());
    observer.observe(node);
    document.addEventListener("visibilitychange", compute);
    window.addEventListener("focus", compute);
    window.addEventListener("resize", compute);
    window.addEventListener("scroll", compute, true);
    compute();
    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", compute);
      window.removeEventListener("focus", compute);
      window.removeEventListener("resize", compute);
      window.removeEventListener("scroll", compute, true);
    };
  }, [ref]);

  return visible;
}

function usePinnedScroll<T extends HTMLElement>(
  dep: unknown,
  resetKey: unknown,
  onViewportChange?: (viewport: { top: number; height: number }, force?: boolean) => void,
  initialPinned = true,
  onScrollStateChange?: (state: { pinned: boolean; scrollTop: number }) => void,
) {
  const ref = useRef<T | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const rafRef = useRef<number | null>(null);
  const pendingForceRef = useRef(false);
  const viewportRef = useRef<{ top: number; height: number } | null>(null);

  const syncViewport = useCallback((force = false) => {
    const el = ref.current;
    if (!el || !onViewportChange) return;
    const height = el.clientHeight || VIRTUAL_DEFAULT_VIEWPORT;
    const top = el.scrollTop;
    const previousViewport = viewportRef.current;
    const nextPinned = el.scrollHeight - top - height < 24;
    if (!(pinnedRef.current && previousViewport && previousViewport.top === top && height < previousViewport.height)) {
      pinnedRef.current = nextPinned;
    }
    const viewport = { top, height };
    viewportRef.current = viewport;
    onViewportChange(viewport, force);
    onScrollStateChange?.({ pinned: pinnedRef.current, scrollTop: top });
  }, [onScrollStateChange, onViewportChange]);

  const scheduleViewportSync = useCallback((force = false) => {
    pendingForceRef.current = pendingForceRef.current || force;
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      const shouldForce = pendingForceRef.current;
      pendingForceRef.current = false;
      syncViewport(shouldForce);
    });
  }, [syncViewport]);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    syncViewport();
  }, [syncViewport]);

  // New target (ticket switch) always starts pinned at the bottom.
  useLayoutEffect(() => {
    pinnedRef.current = initialPinned;
    viewportRef.current = null;
  }, [initialPinned, resetKey]);

  // Pin before paint so an opened log never flashes at the top.
  useLayoutEffect(() => {
    if (pinnedRef.current) scrollToBottom();
    else syncViewport();
  }, [dep, scrollToBottom, syncViewport]);

  // Content can grow after the effect (font swap, wrapping, expands) — re-pin on resize.
  useEffect(() => {
    const el = ref.current;
    const inner = innerRef.current;
    if (!el || !inner) return;
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) scrollToBottom();
      else scheduleViewportSync(true);
    });
    observer.observe(el);
    observer.observe(inner);
    return () => {
      observer.disconnect();
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [dep, scheduleViewportSync, scrollToBottom]);

  useLayoutEffect(() => {
    syncViewport(true);
  }, [resetKey, syncViewport]);

  const handleNativeScroll = useCallback(() => {
    scheduleViewportSync();
  }, [scheduleViewportSync]);
 
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.addEventListener("scroll", handleNativeScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleNativeScroll);
  }, [handleNativeScroll]);
  return {
    ref,
    innerRef,
    pinnedRef,
    scrollToBottom,
    syncViewport,
  };
}

function formatTokens(tokens: number | null): string | null {
  if (!tokens) return null;
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M tok`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k tok`;
  return `${tokens} tok`;
}

function formatDispositionCounts({ unknown }: { unknown: number }): string {
  return `Unknown ${unknown}`;
}

type ProviderQuestion = {
  id: string;
  header?: string;
  question: string;
  isOther?: boolean;
  isSecret?: boolean;
  options?: { label: string; description?: string }[];
};

function providerQuestions(request: ProviderPendingRequest): ProviderQuestion[] {
  if (request.request_kind !== "item/tool/requestUserInput") return [];
  const params = request.payload.params;
  if (!params || typeof params !== "object") return [];
  const questions = (params as { questions?: unknown }).questions;
  return Array.isArray(questions)
    ? questions.filter(
        (question): question is ProviderQuestion =>
          Boolean(
            question &&
              typeof question === "object" &&
              typeof (question as { id?: unknown }).id === "string" &&
              typeof (question as { question?: unknown }).question === "string",
          ),
      )
    : [];
}

function ProviderPendingRequestCard({
  request,
  ticket,
}: {
  request: ProviderPendingRequest;
  ticket: string;
}) {
  const questions = providerQuestions(request);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [responseText, setResponseText] = useState("{}");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (sending || sent) return;
    let response: Record<string, unknown>;
    if (questions.length > 0) {
      const missing = questions.find((question) => !answers[question.id]?.trim());
      if (missing) {
        setError(`Answer required: ${missing.question}`);
        return;
      }
      response = {
        answers: Object.fromEntries(
          questions.map((question) => [
            question.id,
            { answers: [answers[question.id].trim()] },
          ]),
        ),
      };
    } else {
      try {
        const parsed: unknown = JSON.parse(responseText);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("Response must be a JSON object.");
        }
        response = parsed as Record<string, unknown>;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Response is not valid JSON.");
        return;
      }
    }

    setSending(true);
    setError(null);
    try {
      await respondToAgentRequest(ticket, request.request_id, response);
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send provider response.");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="session-provider-request">
      <div className="session-provider-request-head">
        <span>Action required</span>
        <span>{request.request_kind}</span>
        <span>request {String(request.request_id)}</span>
      </div>
      {questions.length > 0 ? (
        <div className="session-provider-questions">
          {questions.map((question) => (
            <label className="session-provider-question" key={question.id}>
              <span>
                {question.header ? `${question.header} · ` : ""}
                {question.question}
              </span>
              {question.options?.length ? (
                <select
                  value={answers[question.id] ?? ""}
                  onChange={(event) => {
                    setAnswers((current) => ({
                      ...current,
                      [question.id]: event.target.value,
                    }));
                    setError(null);
                  }}
                >
                  <option value="">Choose…</option>
                  {question.options.map((option) => (
                    <option key={option.label} value={option.label}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : null}
              {question.isOther || !question.options?.length ? (
                <input
                  placeholder={question.options?.length ? "Or type another answer" : "Answer"}
                  type={question.isSecret ? "password" : "text"}
                  value={answers[question.id] ?? ""}
                  onChange={(event) => {
                    setAnswers((current) => ({
                      ...current,
                      [question.id]: event.target.value,
                    }));
                    setError(null);
                  }}
                />
              ) : null}
            </label>
          ))}
        </div>
      ) : (
        <label className="session-provider-json-response">
          <span>Provider response JSON</span>
          <textarea
            spellCheck={false}
            value={responseText}
            onChange={(event) => {
              setResponseText(event.target.value);
              setError(null);
            }}
          />
        </label>
      )}
      <div className="session-provider-request-actions">
        <details>
          <summary>Raw request</summary>
          <pre>{JSON.stringify(request.payload, null, 2)}</pre>
        </details>
        <button disabled={sending || sent} type="button" onClick={() => void submit()}>
          {sent
            ? "Response sent"
            : sending
              ? "Sending…"
              : questions.length
                ? "Send answers"
                : "Send JSON"}
        </button>
      </div>
      {error ? <div className="session-provider-request-error">{error}</div> : null}
    </div>
  );
}

type QuestionDraft = {
  answers: Record<string, number>;
  sending: boolean;
  error: string | null;
};

type QuestionUiContextValue = {
  drafts: Record<string, QuestionDraft>;
  answerQuestion: (event: SessionEvent, optionIndex: number) => void;
};

const QuestionUiContext = createContext<QuestionUiContextValue | null>(null);

function modelChangedMarkers(session: TranscriptSession | null): SessionEvent[] {
  if (!session?.providerInspector?.events.length) return [];
  return session.providerInspector.events
    .filter((event) => event.kind === "model_changed")
    .map((event) => {
      const toModel = typeof event.payload.to_model === "string" ? event.payload.to_model : "";
      const text =
        typeof event.payload.message === "string"
          ? event.payload.message
          : `model changed to ${toModel || "new model"}`;
      return {
        id: 1_000_000 + event.seq,
        kind: "marker" as const,
        ts: event.normalized_at,
        text,
        disposition: "rendered" as const,
        marker: "model_changed",
      };
    });
}

function SessionModelFooter({
  session,
  ticket,
}: {
  session: TranscriptSession;
  ticket: string;
}) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<AgentModelOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [confirmModel, setConfirmModel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentModel = session.model ?? "";
  const desiredModel = session.desiredModel;
  const kind = session.kind;
  const allowedModels = useMemo(
    () =>
      models.filter(
        (option) => option.kind === kind && option.id !== currentModel,
      ),
    [currentModel, kind, models],
  );

  useEffect(() => {
    if (!open || models.length > 0 || loading) return;
    setLoading(true);
    getAgentModels()
      .then((result) => setModels(result.models ?? []))
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load models"))
      .finally(() => setLoading(false));
  }, [loading, models.length, open]);

  async function confirmSwitch() {
    if (!confirmModel) return;
    setBusy(true);
    setError(null);
    try {
      const result = await setAgentModel(ticket, confirmModel);
      replaceTranscriptDesiredModel(
        ticket,
        result.desired_model === undefined ? confirmModel : result.desired_model,
      );
      setConfirmModel(null);
      setOpen(false);
      invalidateTranscript(ticket, "session");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue model change");
    } finally {
      setBusy(false);
    }
  }

  async function cancelQueued(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    setBusy(true);
    setError(null);
    try {
      await cancelAgentModelChange(ticket);
      replaceTranscriptDesiredModel(ticket, null);
      invalidateTranscript(ticket, "session");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel model change");
    } finally {
      setBusy(false);
    }
  }

  if (!currentModel || !kind) {
    return <span>{currentModel}</span>;
  }

  return (
    <span className="session-model-control">
      <span className={`session-model-badge${desiredModel ? " has-queued" : ""}`}>
        <button
          aria-expanded={open}
          aria-label="Change model"
          className="session-model-current"
          disabled={busy}
          type="button"
          onClick={() => setOpen((value) => !value)}
        >
          <span>{currentModel}</span>
          {desiredModel ? (
            <span className="session-model-queued">· queued: {desiredModel}</span>
          ) : null}
        </button>
        {desiredModel ? (
          <button
            aria-label="Cancel queued model change"
            className="session-model-cancel"
            disabled={busy}
            type="button"
            onClick={(event) => void cancelQueued(event)}
          >
            <X size={10} />
          </button>
        ) : null}
      </span>
      {open ? (
        <div className="session-model-menu">
          {loading ? <div className="session-model-empty">Loading models</div> : null}
          {!loading && allowedModels.length === 0 ? (
            <div className="session-model-empty">No alternate models</div>
          ) : null}
          {allowedModels.map((option) => (
            <button
              className="session-model-option"
              key={option.id}
              type="button"
              onClick={() => setConfirmModel(option.id)}
            >
              <span>{option.label}</span>
              <code>{option.id}</code>
            </button>
          ))}
        </div>
      ) : null}
      {confirmModel ? (
        <div className="session-model-confirm" role="dialog" aria-modal="true">
          <div className="session-model-confirm-text">
            Switch to {confirmModel} after current turn finishes? Currently on {currentModel}.
          </div>
          <div className="session-model-confirm-actions">
            <button type="button" onClick={() => setConfirmModel(null)}>
              Cancel
            </button>
            <button disabled={busy} type="button" onClick={() => void confirmSwitch()}>
              Switch model
            </button>
          </div>
        </div>
      ) : null}
      {error ? <span className="session-model-error">{error}</span> : null}
    </span>
  );
}

function ProviderStreamInspector({
  inspector,
  ticket,
}: {
  inspector: ProviderEventInspector;
  ticket: string;
}) {
  const pendingRequests = inspector.pending_requests ?? [];
  const [open, setOpen] = useState(pendingRequests.length > 0);
  useEffect(() => {
    if (pendingRequests.length > 0) setOpen(true);
  }, [pendingRequests.length]);
  const counts = formatDispositionCounts(inspector.dispositions);
  return (
    <div className={`session-provider-inspector${open ? " is-open" : ""}`}>
      <button
        aria-expanded={open}
        className="session-provider-inspector-head"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <ChevronRight size={12} />
        <span className="session-dispositions-label">Provider stream</span>
        <span className="session-provider-inspector-route">
          raw {inspector.raw_count} → normalized {inspector.normalized_count}
        </span>
        <span className="session-dispositions-value">{counts}</span>
        {pendingRequests.length > 0 ? (
          <span className="session-provider-pending">
            {pendingRequests.length} pending
          </span>
        ) : null}
        <span className={`session-provider-state is-${inspector.state}`}>
          {inspector.provider} · {inspector.state}
        </span>
      </button>
      {open ? (
        <div className="session-provider-inspector-body">
          {pendingRequests.map((request) => (
            <ProviderPendingRequestCard
              key={`${typeof request.request_id}:${request.request_id}`}
              request={request}
              ticket={ticket}
            />
          ))}
          <div className="session-provider-events">
            {inspector.events.length > 0 ? (
              inspector.events
                .slice()
                .reverse()
                .map((event) => (
                  <details className="session-provider-event" key={event.seq}>
                    <summary>
                      <span className={`is-${event.disposition}`}>{event.disposition}</span>
                      <span>#{event.seq}</span>
                      <span>{event.kind}</span>
                      {event.lifecycle_state ? <span>→ {event.lifecycle_state}</span> : null}
                      <span>raw #{event.raw_seq}</span>
                    </summary>
                    <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                  </details>
                ))
            ) : (
              <div className="session-provider-empty">No normalized provider events yet.</div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

const IMG_TOKEN_PATTERN = /\u27e6img:([^\u27e7]+)\u27e7/g;

function splitImgTokens(text: string): (string | { url: string })[] {
  const parts: (string | { url: string })[] = [];
  let last = 0;
  for (const match of text.matchAll(IMG_TOKEN_PATTERN)) {
    if (match.index! > last) parts.push(text.slice(last, match.index));
    parts.push({ url: match[1] });
    last = match.index! + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function UserText({
  text,
  imageNums = [],
}: {
  text: string;
  imageNums?: number[];
}) {
  const parts = splitImgTokens(text);
  const hasImages = parts.some((part) => typeof part !== "string");
  if (!hasImages) return <div className="session-text">{text}</div>;
  let imgIndex = -1;
  return (
    <div className="session-text">
      {parts.map((part, i) => {
        if (typeof part === "string") return <span key={i}>{part}</span>;
        imgIndex += 1;
        return (
          <ImageChip
            key={i}
            num={imageNums[imgIndex] ?? 0}
            url={part.url}
          />
        );
      })}
    </div>
  );
}

const ARCHETYPE_ICONS: Record<string, LucideIcon> = {
  monitor: Eye,
  steer: SendHorizontal,
  agent: Bot,
  ask: MessageCircleQuestion,
  read: FileText,
  search: Search,
  edit: Pencil,
  git: GitCommit,
  github: GitPullRequest,
  validate: CircleCheck,
  wait: Hourglass,
  status: Radio,
  ticket: Tag,
  infra: Server,
  plan: ListTodo,
  run: Terminal,
  tool: Wrench,
};

function ToolRow({
  event,
  onInspect,
  stateKey,
  uiState,
}: {
  event: SessionEvent;
  onInspect?: (agentId: string) => void;
  stateKey: string;
  uiState: SessionUiState;
}) {
  const [open, setOpen] = useStoredBooleanState(uiState, stateKey, false);
  const tool = event.tool!;
  const Icon = ARCHETYPE_ICONS[tool.archetype] ?? Terminal;
  const summary = tool.summary || tool.input.split("\n")[0].slice(0, 120);
  const running = tool.output === null && tool.ok === null;
  const outputSegments =
    tool.output && containsGitHubPreviewUrl(tool.output)
      ? splitGitHubPreviewSegments(tool.output)
      : null;
  return (
    <div className={`session-tool${open ? " is-open" : ""}`}>
      <button className="session-tool-head" type="button" onClick={() => setOpen(!open)}>
        <ChevronRight className={`collapse-icon${open ? "" : " is-collapsed"}`} size={12} />
        <Icon className="session-tool-icon" size={12} />
        <span className="session-tool-summary" title={tool.name}>
          {summary}
        </span>
        {running ? <span className="session-tool-running" title="running" /> : null}
        {tool.ok === false ? <span className="session-tool-err">err</span> : null}
        {tool.agent_id && onInspect ? (
          <span
            className="session-tool-inspect"
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onInspect(tool.agent_id!);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.stopPropagation();
                onInspect(tool.agent_id!);
              }
            }}
          >
            inspect
          </span>
        ) : null}
      </button>
      <div className={`session-collapsible session-tool-collapsible${open ? " is-open" : ""}`}>
        <div className="session-collapsible-inner">
          <div className="session-tool-body">
            {tool.name === "Bash" ? (
              <ShikiCode className="session-tool-input" code={tool.input} lang="bash" />
            ) : (
              <pre>{tool.input}</pre>
            )}
            {outputSegments ? (
              <div className="session-tool-output-blocks">
                {outputSegments.map((segment, index) =>
                  segment.type === "url" ? (
                    <GhPreviewCard key={`${segment.value}:${index}`} url={segment.value} />
                  ) : segment.value ? (
                    <pre className="session-tool-output" key={`text:${index}`}>
                      {renderAnsi(segment.value)}
                    </pre>
                  ) : null
                )}
              </div>
            ) : tool.output ? <pre className="session-tool-output">{renderAnsi(tool.output)}</pre> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

type EventGroup =
  | { kind: "message"; event: SessionEvent; key: number }
  | { kind: "activity"; events: SessionEvent[]; key: number };

type RowMeasurement = {
  refs: readonly SessionEvent[];
  height: number;
};

type VirtualLayout = {
  keys: number[];
  keyToIndex: Map<number, number>;
  tops: number[];
  sizes: number[];
  totalHeight: number;
};

type SessionUiState = {
  booleans: Map<string, boolean>;
};

function createSessionUiState(): SessionUiState {
  return { booleans: new Map() };
}

function useStoredBooleanState(
  store: SessionUiState,
  key: string,
  initial: boolean
): [boolean, (next: boolean | ((current: boolean) => boolean)) => void] {
  const [, forceRender] = useState(0);
  const value = store.booleans.get(key) ?? initial;
  const setValue = useCallback((next: boolean | ((current: boolean) => boolean)) => {
    const current = store.booleans.get(key) ?? initial;
    const resolved = typeof next === "function" ? next(current) : next;
    if (resolved === initial) store.booleans.delete(key);
    else store.booleans.set(key, resolved);
    forceRender((version) => version + 1);
  }, [initial, key, store]);
  return [value, setValue];
}

function groupEventRefs(group: EventGroup): readonly SessionEvent[] {
  return group.kind === "activity" ? group.events : [group.event];
}

function sameEventRefs(prev: readonly SessionEvent[], next: readonly SessionEvent[]): boolean {
  return prev.length === next.length && prev.every((event, index) => event === next[index]);
}

function estimateWrappedLines(text: string, charsPerLine: number): number {
  let total = 0;
  for (const line of text.split("\n")) total += Math.max(1, Math.ceil(line.length / charsPerLine));
  return total;
}

function getEstimatedGroupHeight(group: EventGroup): number {
  if (group.kind === "activity") return 34;
  switch (group.event.kind) {
    case "assistant":
      return Math.max(96, 28 + estimateWrappedLines(group.event.text, 92) * 22);
    case "bash":
      return Math.max(
        76,
        28 +
          estimateWrappedLines(group.event.bash?.input ?? "", 92) * 20 +
          estimateWrappedLines(
            [group.event.bash?.stdout, group.event.bash?.stderr].filter(Boolean).join("\n"),
            104
          ) *
            18
      );
    case "tasks":
      return Math.max(72, 40 + (group.event.tasks?.length ?? 0) * 28);
    case "question":
      return Math.max(132, 56 + ((group.event.question?.options.length ?? 0) * 28));
    case "terminal":
      return Math.max(60, 24 + estimateWrappedLines(group.event.text, 112) * 18);
    case "user":
      return Math.max(52, 20 + estimateWrappedLines(group.event.text, 96) * 20);
    case "image":
      return 44;
    case "notification":
    case "command":
    case "interrupt":
    case "pr":
    case "marker":
      return Math.max(40, 18 + estimateWrappedLines(group.event.text, 92) * 18);
    default:
      return 56;
  }
}

function getMeasuredGroupHeight(group: EventGroup, heights: Map<number, RowMeasurement>): number | null {
  const measurement = heights.get(group.key);
  if (!measurement) return null;
  return measurement.height;
}

function buildVirtualLayout(groups: EventGroup[], heights: Map<number, RowMeasurement>): VirtualLayout {
  const keys = new Array<number>(groups.length);
  const keyToIndex = new Map<number, number>();
  const tops = new Array<number>(groups.length);
  const sizes = new Array<number>(groups.length);
  let offset = 0;
  for (let index = 0; index < groups.length; index += 1) {
    const group = groups[index];
    keys[index] = group.key;
    keyToIndex.set(group.key, index);
    tops[index] = offset;
    const height = getMeasuredGroupHeight(group, heights) ?? getEstimatedGroupHeight(group);
    const size = height + (index === groups.length - 1 ? 0 : VIRTUAL_ROW_GAP);
    sizes[index] = size;
    offset += size;
  }
  return { keys, keyToIndex, tops, sizes, totalHeight: offset };
}

function findFirstVisibleIndex(layout: VirtualLayout, offset: number): number {
  let lo = 0;
  let hi = layout.tops.length - 1;
  let answer = layout.tops.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (layout.tops[mid] + layout.sizes[mid] > offset) {
      answer = mid;
      hi = mid - 1;
    } else {
      lo = mid + 1;
    }
  }
  return answer;
}

function findLastVisibleIndex(layout: VirtualLayout, offset: number): number {
  let lo = 0;
  let hi = layout.tops.length - 1;
  let answer = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (layout.tops[mid] < offset) {
      answer = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return answer;
}

function computeVisibleRange(layout: VirtualLayout, top: number, height: number): { start: number; end: number } {
  if (layout.tops.length === 0) return { start: 0, end: -1 };
  const overscan = Math.max(VIRTUAL_MIN_OVERSCAN, height * VIRTUAL_OVERSCAN_MULTIPLIER);
  const start = findFirstVisibleIndex(layout, Math.max(0, top - overscan));
  const end = findLastVisibleIndex(layout, top + height + overscan);
  return { start, end: Math.max(start, end) };
}

function sameVisibleRange(
  prev: { start: number; end: number },
  next: { start: number; end: number }
): boolean {
  return prev.start === next.start && prev.end === next.end;
}

function findScrollAnchor(layout: VirtualLayout, top: number): { index: number; key: number; offset: number } | null {
  if (layout.tops.length === 0) return null;
  const index = findFirstVisibleIndex(layout, top);
  return {
    index,
    key: layout.keys[index] ?? 0,
    offset: top - layout.tops[index],
  };
}

function sameImageNums(prev?: number[], next?: number[]): boolean {
  if (prev === next) return true;
  if (!prev || !next) return !prev && !next;
  return prev.length === next.length && prev.every((num, index) => num === next[index]);
}

function groupEvents(events: SessionEvent[], offset: number): EventGroup[] {
  const groups: EventGroup[] = [];
  for (let i = 0; i < events.length; i += 1) {
    const event = events[i];
    // Keys are absolute event indices so open/closed state survives window slides.
    if (event.kind !== "tool" && event.kind !== "thinking") {
      groups.push({ kind: "message", event, key: offset + i });
    } else {
      const last = groups[groups.length - 1];
      if (last && last.kind === "activity") {
        last.events.push(event);
      } else {
        groups.push({ kind: "activity", events: [event], key: offset + i });
      }
    }
  }
  return groups;
}

function ImageChip({
  num,
  url,
}: {
  num: number;
  url: string;
}) {
  const [hover, setHover] = useState(false);

  return (
    <span
      className="session-image-wrap"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <a
        className="session-image-chip"
        href={url}
        {...externalLinkProps(url)}
      >
        [Image #{num}]
      </a>
      {hover ? (
        <span className="session-image-pop">
          <img alt={`Image ${num}`} src={url} />
        </span>
      ) : null}
    </span>
  );
}

function SessionMarkdownLink({ href, ...props }: ComponentProps<"a">) {
  if (typeof href === "string" && isGitHubPreviewUrl(href)) {
    return <GhPreviewCard url={href} />;
  }
  return <a href={href} {...props} {...externalLinkProps(href)} />;
}

const sessionMarkdownComponents = {
  a: SessionMarkdownLink,
  pre: MarkdownPre,
};

function BashBlock({
  event,
  stateKey,
  uiState,
}: {
  event: SessionEvent;
  stateKey: string;
  uiState: SessionUiState;
}) {
  const bash = event.bash ?? { input: "", stdout: "", stderr: "" };
  const output = [bash.stdout, bash.stderr].filter(Boolean).join("\n");
  const canCollapse = output.length > 700 || output.split("\n").length > 14;
  const [open, setOpen] = useStoredBooleanState(uiState, stateKey, !canCollapse);
  return (
    <div className={`session-bash${open ? " is-open" : ""}${canCollapse ? " is-collapsible" : ""}`}>
      {bash.input ? (
        <div className="session-bash-command">
          <span className="session-bash-prompt">❯</span>
          <ShikiCode
            className="session-bash-command-code"
            code={bash.input}
            lang="bash"
            transparent
          />
        </div>
      ) : null}
      {output ? (
        <div className={`session-collapsible session-bash-collapsible${open ? " is-open" : ""}`}>
          <div className="session-collapsible-inner">
            <div className="session-bash-body">
              {bash.stdout ? <pre className="session-bash-stdout">{renderAnsi(bash.stdout)}</pre> : null}
              {bash.stderr ? <pre className="session-bash-stderr">{renderAnsi(bash.stderr)}</pre> : null}
            </div>
          </div>
        </div>
      ) : null}
      {canCollapse ? (
        <button className="session-expand" type="button" onClick={() => setOpen((value) => !value)}>
          {open ? "collapse output" : "expand output"}
        </button>
      ) : null}
    </div>
  );
}

function TaskStatusIcon({ status }: { status: string }) {
  if (status === "completed") return <CircleCheck className="task-icon is-done" size={13} />;
  if (status === "in_progress") return <CircleDashed className="task-icon is-progress" size={13} />;
  return <Circle className="task-icon is-open" size={13} />;
}

function TaskListRow({
  event,
  stateKey,
  uiState,
}: {
  event: SessionEvent;
  stateKey: string;
  uiState: SessionUiState;
}) {
  const tasks = event.tasks ?? [];
  const [open, setOpen] = useStoredBooleanState(uiState, stateKey, true);
  return (
    <div className={`session-tasks${open ? " is-open" : ""}`}>
      <button className="session-tasks-head" type="button" onClick={() => setOpen((v) => !v)}>
        <ChevronRight className={`collapse-icon${open ? "" : " is-collapsed"}`} size={12} />
        <ListTodo size={12} />
        <span className="session-tasks-summary">{event.text}</span>
      </button>
      <div className={`session-collapsible session-tasks-collapsible${open ? " is-open" : ""}`}>
        <div className="session-collapsible-inner">
          <ul className="session-tasks-list">
            {tasks.map((task) => (
              <li className={`session-task is-${task.status}`} key={task.id}>
                <TaskStatusIcon status={task.status} />
                <span className="session-task-id">#{task.id}</span>
                <span className="session-task-subject">
                  {task.status === "in_progress" && task.activeForm ? task.activeForm : task.subject}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function InterruptRow({ text }: { text: string }) {
  return (
    <div className="session-interrupt">
      <CircleSlash size={12} />
      <span>{text}</span>
    </div>
  );
}

function PrRow({ pr, text }: { pr: SessionPr | undefined; text: string }) {
  if (!pr) return null;
  return (
    <a className="session-pr-chip" href={pr.url} {...externalLinkProps(pr.url)}>
      <GitPullRequest size={12} />
      <span>{text}</span>
    </a>
  );
}

function MarkerRow({ text, marker }: { text: string; marker?: string }) {
  const Icon =
    marker === "api_error"
      ? AlertTriangle
      : marker === "permission-mode"
        ? Lock
        : marker === "progress" || marker === "subagent"
          ? Bot
          : marker === "tool_reference"
            ? Wrench
            : Radio;
  return (
    <div className={`session-marker is-${marker ?? "info"}`}>
      <Icon size={12} />
      <span>{text}</span>
    </div>
  );
}

function QuestionRow({ event }: { event: SessionEvent }) {
  const questionUi = useContext(QuestionUiContext);
  const question = event.question;
  if (!question) return null;
  const draft = questionUi?.drafts[question.tool_use_id];
  const optimisticIndex = draft?.answers[question.prompt];
  const pickedIndex =
    question.answered_option !== null ? question.answered_option : optimisticIndex ?? null;
  const answered = pickedIndex !== null ? question.options[pickedIndex] ?? null : null;
  const disabled =
    question.answered_option !== null ||
    Boolean(question.custom_reply) ||
    Boolean(draft?.sending);
  return (
    <div className="session-question">
      <div className="session-question-head">
        <MessageCircleQuestion size={13} />
        <span>{question.header || "Question"}</span>
      </div>
      <div className="session-question-prompt">{question.prompt}</div>
      <div className="session-question-options">
        {question.options.map((option, index) => (
          <button
            className={[
              "session-question-option",
              pickedIndex === index ? "is-picked" : "",
              !disabled ? "is-clickable" : "",
            ].filter(Boolean).join(" ")}
            disabled={disabled}
            key={`${question.prompt}:${option}:${index}`}
            type="button"
            onClick={() => {
              if (disabled) return;
              questionUi?.answerQuestion(event, index);
            }}
          >
            <span className="session-question-index">{index + 1}</span>
            <span>{option}</span>
          </button>
        ))}
      </div>
      {answered ? (
        <div className="session-question-answer">
          <span className="session-question-answer-label">Picked</span>
          <span>{answered}</span>
        </div>
      ) : null}
      {question.custom_reply ? (
        <div className="session-question-custom">
          <span className="session-question-answer-label">Custom reply</span>
          <span>{question.custom_reply}</span>
        </div>
      ) : null}
      {draft?.error ? <div className="session-question-error">{draft.error}</div> : null}
    </div>
  );
}

const MessageBlock = memo(function MessageBlock({
  event,
  imageNums,
  rowKey,
  uiState,
}: {
  event: SessionEvent;
  imageNums?: number[];
  rowKey: number;
  uiState: SessionUiState;
}) {
  if (event.kind === "user") {
    return (
      <div className="session-user">
        <UserText imageNums={imageNums} text={event.text} />
      </div>
    );
  }
  if (event.kind === "terminal") {
    return <pre className="session-pane-log">{renderAnsi(event.text)}</pre>;
  }
  if (event.kind === "image") {
    return (
      <div>
        <ImageChip num={imageNums?.[0] ?? 0} url={event.text} />
      </div>
    );
  }
  if (event.kind === "notification") {
    return (
      <div className="session-notification">
        <Bell size={12} />
        <span>{event.text}</span>
      </div>
    );
  }
  if (event.kind === "command") {
    return (
      <div className="session-command">
        <SlashSquare size={12} />
        <span>{event.text}</span>
      </div>
    );
  }
  if (event.kind === "bash") {
    return <BashBlock event={event} stateKey={`bash:${rowKey}`} uiState={uiState} />;
  }
  if (event.kind === "tasks") {
    return <TaskListRow event={event} stateKey={`tasks:${rowKey}`} uiState={uiState} />;
  }
  if (event.kind === "question") {
    return <QuestionRow event={event} />;
  }
  if (event.kind === "interrupt") {
    return <InterruptRow text={event.text} />;
  }
  if (event.kind === "pr") {
    return <PrRow pr={event.pr} text={event.text} />;
  }
  if (event.kind === "marker") {
    return <MarkerRow marker={event.marker} text={event.text} />;
  }
  return (
    <div className="session-assistant markdown-preview-view">
      <ReactMarkdown components={sessionMarkdownComponents} remarkPlugins={[remarkGfm]}>
        {event.text}
      </ReactMarkdown>
    </div>
  );
}, (prev, next) =>
  prev.event === next.event &&
  prev.rowKey === next.rowKey &&
  prev.uiState === next.uiState &&
  sameImageNums(prev.imageNums, next.imageNums)
);

function ActivityGroupBase({
  events,
  groupKey,
  onInspect,
  uiState,
}: {
  events: SessionEvent[];
  groupKey: number;
  onInspect?: (agentId: string) => void;
  uiState: SessionUiState;
}) {
  const [open, setOpen] = useStoredBooleanState(uiState, `activity:${groupKey}`, false);
  const tools = events.filter((e) => e.kind === "tool");
  const thinking = events.filter((e) => e.kind === "thinking");
  const parts: string[] = [];
  if (tools.length) parts.push(`${tools.length} tool call${tools.length > 1 ? "s" : ""}`);
  if (thinking.length) parts.push(`${thinking.length} thinking`);
  return (
    <div className="session-activity">
      <button className="session-activity-head" type="button" onClick={() => setOpen(!open)}>
        <ChevronRight className={`collapse-icon${open ? "" : " is-collapsed"}`} size={12} />
        {parts.join(" · ") || "activity"}
      </button>
      <div className={`session-collapsible session-activity-collapsible${open ? " is-open" : ""}`}>
        <div className="session-collapsible-inner">
          <div className="session-activity-body">
            {events.map((event, index) => {
              if (event.kind === "tool") {
                return (
                  <ToolRow
                    event={event}
                    key={groupKey + index}
                    onInspect={onInspect}
                    stateKey={`tool:${groupKey + index}`}
                    uiState={uiState}
                  />
                );
              }
              if (!event.text) return null;
              return (
                <div className="session-thinking" key={groupKey + index}>
                  {event.encrypted ? <span className="session-thinking-chip">encrypted</span> : null}
                  {event.text}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

const ActivityGroup = memo(ActivityGroupBase, (prev, next) =>
  prev.groupKey === next.groupKey &&
  prev.onInspect === next.onInspect &&
  prev.uiState === next.uiState &&
  prev.events.length === next.events.length &&
  prev.events.every((event, index) => event === next.events[index])
);

function useMeasuredRow(group: EventGroup, onHeightChange: (group: EventGroup, height: number) => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const report = (height: number) => onHeightChange(group, Math.max(MIN_ROW_HEIGHT, Math.round(height)));
    report(el.getBoundingClientRect().height);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) report(entry.contentRect.height);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [group, onHeightChange]);
  return ref;
}

function groupTimestamp(group: EventGroup): string | null {
  if (group.kind === "message") return group.event.ts;
  for (const event of group.events) {
    if (event.ts) return event.ts;
  }
  return null;
}

function groupAlign(group: EventGroup): "end" | "start" {
  return group.kind === "message" && group.event.kind === "user" ? "end" : "start";
}

function computeTimestampKeys(groups: EventGroup[]): Set<number> {
  const keys = new Set<number>();
  for (let i = 0; i < groups.length; i += 1) {
    const g = groups[i];
    if (g.kind !== "message") continue;
    const kind = g.event.kind;
    if (kind === "user") {
      keys.add(g.key);
      continue;
    }
    if (kind !== "assistant") continue;
    let isLast = true;
    for (let j = i + 1; j < groups.length; j += 1) {
      const later = groups[j];
      if (later.kind !== "message") continue;
      const laterKind = later.event.kind;
      if (laterKind === "assistant") {
        isLast = false;
        break;
      }
      if (laterKind === "user") break;
    }
    if (isLast) keys.add(g.key);
  }
  return keys;
}

const VirtualSessionRow = memo(function VirtualSessionRow({
  group,
  imageNums,
  onHeightChange,
  onInspect,
  showTimestamp,
  top,
  uiState,
}: {
  group: EventGroup;
  imageNums?: number[];
  onHeightChange: (group: EventGroup, height: number) => void;
  onInspect?: (agentId: string) => void;
  showTimestamp: boolean;
  top: number;
  uiState: SessionUiState;
}) {
  const rowRef = useMeasuredRow(group, onHeightChange);
  const style: CSSProperties = { transform: `translateY(${top}px)` };
  const ts = showTimestamp ? groupTimestamp(group) : null;
  return (
    <div
      className="session-virtual-row"
      data-align={groupAlign(group)}
      data-group-key={group.key}
      data-row-top={top}
      ref={rowRef}
      style={style}
    >
      {group.kind === "activity" ? (
        <ActivityGroup events={group.events} groupKey={group.key} onInspect={onInspect} uiState={uiState} />
      ) : (
        <MessageBlock
          event={group.event}
          imageNums={imageNums}
          rowKey={group.key}
          uiState={uiState}
        />
      )}
      {ts ? <Timestamp value={ts} /> : null}
    </div>
  );
}, (prev, next) => {
  if (
    prev.top !== next.top ||
    prev.onHeightChange !== next.onHeightChange ||
    prev.onInspect !== next.onInspect ||
    prev.showTimestamp !== next.showTimestamp ||
    prev.uiState !== next.uiState
  ) {
    return false;
  }
  const prevGroup = prev.group;
  const nextGroup = next.group;
  if (prevGroup.kind !== nextGroup.kind || prevGroup.key !== nextGroup.key) return false;
  if (prevGroup.kind === "activity" && nextGroup.kind === "activity") {
    return (
      prevGroup.events.length === nextGroup.events.length &&
      prevGroup.events.every((event, index) => event === nextGroup.events[index])
    );
  }
  if (prevGroup.kind !== "message" || nextGroup.kind !== "message") return false;
  return prevGroup.event === nextGroup.event && sameImageNums(prev.imageNums, next.imageNums);
});

type SessionAcc = TranscriptSession;

type ComposerMode = "insert" | "normal" | "visual" | "pane";

type ComposerState = {
  text: string;
  vimMode: ComposerMode;
  selectionStart: number;
  selectionEnd: number;
};

type ScrollAnchor = {
  index: number;
  key: number;
  offset: number;
};

type ScrollState = {
  pinned: boolean;
  scrollTop: number;
  anchor: ScrollAnchor | null;
};

const composerStateCache = new Map<string, ComposerState>();
const sessionUiStateCache = new Map<string, SessionUiState>();
const sessionScrollCache = new Map<string, ScrollState>();
const sessionScrollWriteBarrier = createStateKeyWriteBarrier();

function composerStateKeyForSession(ticket: string, subagent?: string): string {
  return subagent
    ? `${ticket}:subagent:${subagent}:composer`
    : `${ticket}:main:composer`;
}

export function clearSessionPaneState(paneStateKey: string) {
  deletePaneStateEntries(sessionUiStateCache, paneStateKey);
  sessionScrollWriteBarrier.block(deletePaneStateEntries(sessionScrollCache, paneStateKey));
}

function getComposerState(key: string): ComposerState | null {
  return composerStateCache.get(key) ?? null;
}

function setComposerState(key: string, state: ComposerState) {
  const isDefault =
    state.text.length === 0 &&
    state.vimMode === "insert" &&
    state.selectionStart === 0 &&
    state.selectionEnd === 0;
  if (isDefault) composerStateCache.delete(key);
  else composerStateCache.set(key, state);
}

function getSessionUiState(key: string): SessionUiState {
  const existing = sessionUiStateCache.get(key);
  if (existing) return existing;
  const created = createSessionUiState();
  sessionUiStateCache.set(key, created);
  return created;
}

function decrementRestoreAttempts(ref: { current: number }, pendingRef: { current: boolean }) {
  ref.current -= 1;
  if (ref.current <= 0) pendingRef.current = false;
}

function setSessionScrollState(key: string, state: ScrollState) {
  if (!sessionScrollWriteBarrier.allows(key)) return;
  sessionScrollCache.set(key, state);
}

function resolveScrollAnchorTarget(
  layout: VirtualLayout,
  anchor: ScrollAnchor,
  viewportHeight: number
): { index: number; key: number; target: number } | null {
  if (layout.tops.length === 0) return null;
  const index = layout.keyToIndex.get(anchor.key) ?? Math.min(anchor.index, Math.max(0, layout.tops.length - 1));
  const key = layout.keys[index];
  if (key === undefined) return null;
  const maxScrollTop = Math.max(0, layout.totalHeight - viewportHeight);
  const target = Math.max(0, Math.min((layout.tops[index] ?? 0) + anchor.offset, maxScrollTop));
  return { index, key, target };
}

export function SessionTab({
  ticket,
  subagent,
  showComposer = true,
  onInspect,
  stateKey,
}: {
  ticket: string;
  subagent?: string;
  showComposer?: boolean;
  onInspect?: (agentId: string) => void;
  stateKey?: string;
}) {
  const resetKey = `${ticket}:${subagent ?? ""}`;
  const sessionStateKey = `${stateKey ?? resetKey}:${resetKey}`;
  const rowHeightsKeyRef = useRef(resetKey);
  const rowHeightsRef = useRef<Map<number, RowMeasurement>>(new Map());
  const layoutRef = useRef<VirtualLayout | null>(null);
  const layoutResetKeyRef = useRef(resetKey);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const restoreAttemptsRef = useRef(3);
  const scrollRestoreStateRef = useRef<ScrollState | null>(sessionScrollCache.get(sessionStateKey) ?? null);
  const scrollRestorePendingRef = useRef(Boolean(scrollRestoreStateRef.current));
  const latestScrollStateRef = useRef<ScrollState | null>(scrollRestoreStateRef.current);
  const rowHeightVersionRef = useRef(0);
  const restoreFinalizeRafRef = useRef<number | null>(null);
  const [rowHeightVersion, setRowHeightVersion] = useState(0);
  const uiState = useMemo(() => getSessionUiState(sessionStateKey), [sessionStateKey]);
  const cachedScroll = scrollRestoreStateRef.current;

  rowHeightVersionRef.current = rowHeightVersion;

  if (rowHeightsKeyRef.current !== resetKey) {
    rowHeightsKeyRef.current = resetKey;
    rowHeightsRef.current = new Map();
  }

  const target = useMemo(
    () => (subagent ? { ticket, subagent } : { ticket }),
    [subagent, ticket]
  );
  const visible = useElementVisible(containerRef);
  const { session, error, loading } = useTranscriptSession(target, visible);
  const [questionDrafts, setQuestionDrafts] = useState<Record<string, QuestionDraft>>({});

  const questionGroups = useMemo(() => {
    const groups = new Map<string, SessionEvent[]>();
    for (const event of session?.events ?? []) {
      if (event.kind !== "question" || !event.question?.tool_use_id) continue;
      const existing = groups.get(event.question.tool_use_id);
      if (existing) existing.push(event);
      else groups.set(event.question.tool_use_id, [event]);
    }
    return groups;
  }, [session?.events]);

  useEffect(() => {
    setQuestionDrafts((current) => {
      let changed = false;
      const next: Record<string, QuestionDraft> = {};
      for (const [toolUseId, draft] of Object.entries(current)) {
        const events = questionGroups.get(toolUseId);
        if (!events || events.every((event) => {
          const question = event.question;
          return Boolean(
            question &&
            (question.answered_option !== null || question.custom_reply)
          );
        })) {
          changed = true;
          continue;
        }
        next[toolUseId] = draft;
      }
      return changed ? next : current;
    });
  }, [questionGroups]);

  const answerQuestion = useCallback(async (event: SessionEvent, optionIndex: number) => {
    const question = event.question;
    const toolUseId = question?.tool_use_id;
    if (
      !question ||
      !toolUseId ||
      question.answered_option !== null ||
      question.custom_reply
    ) {
      return;
    }
    const group = questionGroups.get(toolUseId) ?? [event];
    const previous = questionDrafts[toolUseId] ?? { answers: {}, sending: false, error: null };
    if (previous.sending) return;
    const nextAnswers = { ...previous.answers, [question.prompt]: optionIndex };
    const payloadAnswers = group.map((candidate) => {
      const candidateQuestion = candidate.question;
      if (!candidateQuestion) return null;
      const selectedIndex =
        candidateQuestion.answered_option !== null
          ? candidateQuestion.answered_option
          : nextAnswers[candidateQuestion.prompt];
      if (selectedIndex === undefined || selectedIndex === null) return null;
      const answer = candidateQuestion.options[selectedIndex];
      if (!answer) return null;
      return [candidateQuestion.prompt, answer] as const;
    });
    const ready = payloadAnswers.every((candidate) => candidate !== null);
    setQuestionDrafts((current) => ({
      ...current,
      [toolUseId]: {
        answers: nextAnswers,
        sending: ready,
        error: null,
      },
    }));
    if (!ready) return;
    const readyAnswers = payloadAnswers.filter(
      (candidate): candidate is readonly [string, string] => candidate !== null,
    );
    try {
      await respondToAgentRequest(ticket, toolUseId, {
        answers: Object.fromEntries(readyAnswers),
      });
      setQuestionDrafts((current) => ({
        ...current,
        [toolUseId]: {
          answers: nextAnswers,
          sending: false,
          error: null,
        },
      }));
    } catch (submitError) {
      setQuestionDrafts((current) => ({
        ...current,
        [toolUseId]: {
          answers: previous.answers,
          sending: false,
          error:
            submitError instanceof Error
              ? submitError.message
              : "Could not answer question.",
        },
      }));
    }
  }, [questionDrafts, questionGroups, ticket]);

  const questionUi = useMemo<QuestionUiContextValue>(() => ({
    drafts: questionDrafts,
    answerQuestion,
  }), [answerQuestion, questionDrafts]);

  const displayEvents = useMemo(
    () => {
      if (!session) return [];
      return [...session.events, ...modelChangedMarkers(session)];
    },
    [session],
  );

  const groups = useMemo(
    () => groupEvents(displayEvents, session?.base ?? 0),
    [displayEvents, session?.base]
  );
  const layout = useMemo(
    () => buildVirtualLayout(groups, rowHeightsRef.current),
    [groups, resetKey, rowHeightVersion]
  );
  const [visibleRange, setVisibleRange] = useState<{ start: number; end: number }>({ start: 0, end: -1 });
  const visibleRangeViewportRef = useRef<{ top: number; height: number } | null>(null);
  const syncVisibleRange = useCallback((viewport: { top: number; height: number }, force = false) => {
    const height = viewport.height || VIRTUAL_DEFAULT_VIEWPORT;
    const overscan = Math.max(VIRTUAL_MIN_OVERSCAN, height * VIRTUAL_OVERSCAN_MULTIPLIER);
    const previousViewport = visibleRangeViewportRef.current;
    if (!force && previousViewport && previousViewport.height === height) {
      const hysteresis = overscan * 0.5;
      if (Math.abs(viewport.top - previousViewport.top) < hysteresis) return;
    }
    visibleRangeViewportRef.current = { top: viewport.top, height };
    const next = computeVisibleRange(layout, viewport.top, height);
    setVisibleRange((current) => (sameVisibleRange(current, next) ? current : next));
  }, [layout]);
  const {
    ref,
    innerRef,
    pinnedRef,
    scrollToBottom,
    syncViewport,
  } = usePinnedScroll<HTMLDivElement>(
    layout.totalHeight,
    resetKey,
    syncVisibleRange,
    cachedScroll?.pinned ?? true,
    useCallback((state: { pinned: boolean; scrollTop: number }) => {
      const viewportLayout = layoutRef.current ?? layout;
      const anchor = state.pinned ? null : findScrollAnchor(viewportLayout, state.scrollTop);
      const nextState = {
        ...state,
        anchor,
      };
      latestScrollStateRef.current = nextState;
      if (scrollRestorePendingRef.current) return;
      setSessionScrollState(sessionStateKey, nextState);
    }, [layout, sessionStateKey])
  );

  useEffect(() => {
    restoreAttemptsRef.current = 3;
    scrollRestoreStateRef.current = sessionScrollCache.get(sessionStateKey) ?? null;
    scrollRestorePendingRef.current = Boolean(scrollRestoreStateRef.current);
    latestScrollStateRef.current = scrollRestoreStateRef.current;
    if (restoreFinalizeRafRef.current !== null) {
      window.cancelAnimationFrame(restoreFinalizeRafRef.current);
      restoreFinalizeRafRef.current = null;
    }
  }, [sessionStateKey]);

  const reportRowHeight = useCallback((group: EventGroup, height: number) => {
    const measurement: RowMeasurement = {
      height,
      refs: group.kind === "activity" ? group.events.slice() : [group.event],
    };
    const current = rowHeightsRef.current.get(group.key);
    if (current && current.height === measurement.height && sameEventRefs(current.refs, measurement.refs)) {
      return;
    }
    rowHeightsRef.current.set(group.key, measurement);
    setRowHeightVersion((version) => version + 1);
  }, []);

  useLayoutEffect(() => {
    const el = ref.current;
    const previousLayout = layoutRef.current;
    const previousResetKey = layoutResetKeyRef.current;
    layoutRef.current = layout;
    layoutResetKeyRef.current = resetKey;
    if (!el) return;
    if (!previousLayout || previousResetKey !== resetKey) {
      if (pinnedRef.current) scrollToBottom();
      else syncViewport(true);
      return;
    }
    if (pinnedRef.current) {
      scrollToBottom();
      return;
    }
    const anchor = findScrollAnchor(previousLayout, el.scrollTop);
    if (!anchor) {
      syncViewport(true);
      return;
    }
    const nextIndex = layout.keyToIndex.get(anchor.key) ?? Math.min(anchor.index, Math.max(0, layout.tops.length - 1));
    const nextTop = layout.tops[nextIndex] ?? 0;
    const maxScrollTop = Math.max(0, layout.totalHeight - el.clientHeight);
    const target = Math.max(0, Math.min(nextTop + anchor.offset, maxScrollTop));
    if (Math.abs(el.scrollTop - target) > 1) el.scrollTop = target;
    syncViewport(true);
  }, [layout, pinnedRef, ref, resetKey, scrollToBottom, syncViewport]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!cachedScroll || !el || !session || restoreAttemptsRef.current <= 0) return;
    const frame = window.requestAnimationFrame(() => {
      const node = ref.current;
      if (!node || restoreAttemptsRef.current <= 0) return;
      if (cachedScroll.pinned) {
        pinnedRef.current = true;
        node.scrollTop = node.scrollHeight;
        syncViewport(true);
        const distance = Math.abs(node.scrollHeight - node.scrollTop - node.clientHeight);
        latestScrollStateRef.current = {
          pinned: true,
          scrollTop: node.scrollTop,
          anchor: null,
        };
        if (distance <= 24) {
          restoreAttemptsRef.current = 0;
          scrollRestorePendingRef.current = false;
        } else decrementRestoreAttempts(restoreAttemptsRef, scrollRestorePendingRef);
        return;
      }
      pinnedRef.current = false;
      const resolvedAnchor = cachedScroll.anchor
        ? resolveScrollAnchorTarget(layout, cachedScroll.anchor, node.clientHeight)
        : null;
      const maxScrollTop = Math.max(0, layout.totalHeight - node.clientHeight);
      const target = resolvedAnchor
        ? resolvedAnchor.target
        : Math.max(0, Math.min(cachedScroll.scrollTop, maxScrollTop));
      if (Math.abs(node.scrollTop - target) > 1) node.scrollTop = target;
      syncViewport(true);
      const restoredAnchor = findScrollAnchor(layout, node.scrollTop);
      latestScrollStateRef.current = {
        pinned: false,
        scrollTop: node.scrollTop,
        anchor: restoredAnchor,
      };
      const anchorRestored = Boolean(
        resolvedAnchor &&
          restoredAnchor &&
          restoredAnchor.key === resolvedAnchor.key &&
          Math.abs(restoredAnchor.offset - (cachedScroll.anchor?.offset ?? 0)) <= 24
      );
      if (anchorRestored || Math.abs(node.scrollTop - target) <= 24) {
        if (restoreFinalizeRafRef.current !== null) {
          window.cancelAnimationFrame(restoreFinalizeRafRef.current);
          restoreFinalizeRafRef.current = null;
        }
        const settledVersion = rowHeightVersion;
        restoreFinalizeRafRef.current = window.requestAnimationFrame(() => {
          restoreFinalizeRafRef.current = window.requestAnimationFrame(() => {
            restoreFinalizeRafRef.current = null;
            if (rowHeightVersionRef.current !== settledVersion) return;
            restoreAttemptsRef.current = 0;
            scrollRestorePendingRef.current = false;
            const finalized = latestScrollStateRef.current ?? {
              pinned: false,
              scrollTop: node.scrollTop,
              anchor: findScrollAnchor(layout, node.scrollTop),
            };
            latestScrollStateRef.current = finalized;
            setSessionScrollState(sessionStateKey, finalized);
          });
        });
      } else decrementRestoreAttempts(restoreAttemptsRef, scrollRestorePendingRef);
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (restoreFinalizeRafRef.current !== null) {
        window.cancelAnimationFrame(restoreFinalizeRafRef.current);
        restoreFinalizeRafRef.current = null;
      }
    };
  }, [cachedScroll, layout, pinnedRef, ref, rowHeightVersion, session, sessionStateKey, syncViewport]);

  useEffect(() => {
    return () => {
      scrollRestorePendingRef.current = false;
      const node = ref.current;
      const latest = latestScrollStateRef.current;
      const scrollTop = node?.scrollTop ?? latest?.scrollTop ?? 0;
      const pinned = latest?.pinned ?? pinnedRef.current;
      const anchor = pinned
        ? null
        : node
          ? findScrollAnchor(layoutRef.current ?? layout, scrollTop)
          : latest?.anchor ?? null;
      const saved = {
        pinned,
        scrollTop,
        anchor,
      };
      if (sessionScrollWriteBarrier.consume(sessionStateKey)) return;
      sessionScrollCache.set(sessionStateKey, saved);
    };
  }, [pinnedRef, ref, sessionStateKey]);
  const visibleGroups = useMemo(() => {
    const end = Math.min(visibleRange.end, groups.length - 1);
    if (end < visibleRange.start) return [];
    const rows: { group: EventGroup; top: number }[] = [];
    for (let index = visibleRange.start; index <= end; index += 1) {
      const group = groups[index];
      if (!group) continue;
      rows.push({ group, top: layout.tops[index] ?? 0 });
    }
    return rows;
  }, [groups, layout.tops, visibleRange.end, visibleRange.start]);
  const timestampKeys = useMemo(() => computeTimestampKeys(groups), [groups]);

  const imageNumbers = useMemo(() => {
    const map = new Map<SessionEvent, number[]>();
    let n = 0;
    for (const event of session?.events ?? []) {
      if (event.kind === "image") {
        map.set(event, [(n += 1)]);
      } else if (event.kind === "user") {
        const count = [...event.text.matchAll(IMG_TOKEN_PATTERN)].length;
        if (count > 0) map.set(event, Array.from({ length: count }, () => (n += 1)));
      }
    }
    return map;
  }, [session]);

  const runningSubagents = useMemo(
    () => (session?.subagents ?? []).filter((entry) => entry.active),
    [session]
  );

  // K/J recall in the composer — user messages from the transcript itself
  // (covers terminal-typed AND wiki-sent, no separate storage).
  const userHistory = useMemo(
    () =>
      (session?.events ?? [])
        .filter((event) => event.kind === "user" && event.text.length <= 2000)
        .map((event) => event.text)
        .slice(-50),
    [session]
  );

  const taskCounts = useMemo(() => {
    const c = { total: 0, done: 0, progress: 0, open: 0 };
    for (const task of session?.tasks ?? []) {
      c.total += 1;
      if (task.status === "completed") c.done += 1;
      else if (task.status === "in_progress") c.progress += 1;
      else c.open += 1;
    }
    return c;
  }, [session]);

  if (!session) {
    if (error && !loading) {
      return (
        <div className="session-tab" ref={containerRef}>
          <div className="session-empty">{error}</div>
        </div>
      );
    }
    return (
      <div className="session-tab" ref={containerRef}>
        <div className="session-empty">
          <LoadingPlaceholder className="session-loading" lines={[82, 96, 74, 88]} />
        </div>
      </div>
    );
  }

  const tokens = formatTokens(session.tokens);
  const dispositionCounts = formatDispositionCounts(session.dispositions);
  const footerSegments = [session.format, tokens ?? "", dispositionCounts].filter(Boolean);

  return (
    <QuestionUiContext.Provider value={questionUi}>
      <div className="session-tab" ref={containerRef}>
      {(session.tasks.length > 0 || session.pr || session.sessionMeta.custom_title || session.sessionMeta.agent_name) ? (
        <div className="session-state-strip">
          {session.sessionMeta.custom_title ? (
            <span className="session-state-meta">{session.sessionMeta.custom_title}</span>
          ) : null}
          {session.sessionMeta.agent_name ? (
            <span className="session-state-meta is-faint">{session.sessionMeta.agent_name}</span>
          ) : null}
          {session.tasks.length > 0 ? (
            <span className="session-state-tasks">
              <ListTodo size={12} />
              <span>
                {taskCounts.total} task{taskCounts.total === 1 ? "" : "s"} · {taskCounts.done} done
                {taskCounts.progress ? ` · ${taskCounts.progress} in progress` : ""}
                {taskCounts.open ? ` · ${taskCounts.open} open` : ""}
              </span>
            </span>
          ) : null}
          {session.pr ? (
            <a
              className="session-state-pr"
              href={session.pr.url}
              {...externalLinkProps(session.pr.url)}
            >
              <GitPullRequest size={12} />
              PR #{session.pr.number}
            </a>
          ) : null}
        </div>
      ) : null}
      <div className="session-dispositions">
        <span className="session-dispositions-value">{dispositionCounts}</span>
      </div>
      {session.providerInspector ? (
        <ProviderStreamInspector inspector={session.providerInspector} ticket={ticket} />
      ) : null}
      <div className="session-scroll" ref={ref}>
        <div className="session-scroll-inner" ref={innerRef}>
          <div className="session-virtual-list" style={{ height: layout.totalHeight }}>
            {visibleGroups.map(({ group, top }) => (
              <VirtualSessionRow
                group={group}
                imageNums={group.kind === "message" ? imageNumbers.get(group.event) : undefined}
                key={group.key}
                onHeightChange={reportRowHeight}
                onInspect={onInspect}
                showTimestamp={timestampKeys.has(group.key)}
                top={top}
                uiState={uiState}
              />
            ))}
          </div>
        </div>
      </div>
      {subagent || !showComposer ? null : (
        <MessageComposer
          history={userHistory}
          queued={session.queue}
          runningSubagents={runningSubagents}
          stateKey={composerStateKeyForSession(ticket, subagent)}
          thinking={session.working}
          ticket={ticket}
          onInspect={onInspect}
        />
      )}
      <div className="session-footer tabular-nums">
        {footerSegments[0] ? <span>{footerSegments[0]}</span> : null}
        <span className="session-footer-separator">·</span>
        <SessionModelFooter session={session} ticket={ticket} />
        {footerSegments.slice(1).map((segment) => (
          <span className="session-footer-segment" key={segment}>
            <span className="session-footer-separator">·</span>
            {segment}
          </span>
        ))}
      </div>
      </div>
    </QuestionUiContext.Provider>
  );
}

function wordRight(text: string, at: number): number {
  const rest = text.slice(at);
  const match = rest.match(/^\s*\S+\s*/);
  return match ? Math.min(at + match[0].length, text.length) : text.length;
}

function wordLeft(text: string, at: number): number {
  const before = text.slice(0, at);
  const match = before.match(/\S+\s*$/);
  return match ? at - match[0].length : 0;
}

function wordEnd(text: string, at: number): number {
  const rest = text.slice(at + 1);
  const match = rest.match(/^\s*\S+/);
  return match ? at + match[0].length : text.length - 1;
}

function lineBounds(text: string, at: number): { start: number; end: number } {
  const start = text.lastIndexOf("\n", at - 1) + 1;
  const lineEnd = text.indexOf("\n", at);
  return { start, end: lineEnd === -1 ? text.length : lineEnd };
}

function lineMove(text: string, at: number, dir: 1 | -1): number {
  const { start, end } = lineBounds(text, at);
  const col = at - start;
  if (dir === 1) {
    if (end >= text.length) return at;
    const next = lineBounds(text, end + 1);
    return Math.min(next.start + col, Math.max(next.start, next.end - 1));
  }
  if (start === 0) return at;
  const prev = lineBounds(text, start - 2 < 0 ? 0 : start - 1);
  return Math.min(prev.start + col, Math.max(prev.start, prev.end - 1));
}

function MessageComposer({
  stateKey,
  ticket,
  history = [],
  queued = [],
  runningSubagents = [],
  thinking = false,
  onInspect,
}: {
  stateKey: string;
  ticket: string;
  history?: string[];
  queued?: QueuedMessage[];
  runningSubagents?: SubagentInfo[];
  thinking?: boolean;
  onInspect?: (agentId: string) => void;
}) {
  const cachedComposer = getComposerState(stateKey);
  const selectionRef = useRef({
    start: cachedComposer?.selectionStart ?? cachedComposer?.text.length ?? 0,
    end: cachedComposer?.selectionEnd ?? cachedComposer?.text.length ?? 0,
  });
  const restoreSelectionRef = useRef(true);
  const [text, setText] = useState(() => cachedComposer?.text ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [vimMode, setVimMode] = useState<ComposerMode>(() => cachedComposer?.vimMode ?? "insert");
  const pendingKeyRef = useRef<string | null>(null);
  const registerRef = useRef<string>("");
  const historyPosRef = useRef<number | null>(null);
  const draftRef = useRef("");
  const [attachments, setAttachments] = useState<{ path: string; url: string }[]>([]);
  // (inputRef & visual refs declared below with the menu state)
  const skills = useSkills();
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const visualAnchorRef = useRef(0);
  const visualHeadRef = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const measureRef = useRef<HTMLTextAreaElement | null>(null);
  const [caretPos, setCaretPos] = useState(selectionRef.current.start);
  const [overlayPos, setOverlayPos] = useState<{ top: number; left: number } | null>(null);

  useEffect(() => {
    const cached = getComposerState(stateKey);
    const nextText = cached?.text ?? "";
    setText(nextText);
    setVimMode(cached?.vimMode ?? "insert");
    selectionRef.current = {
      start: cached?.selectionStart ?? nextText.length,
      end: cached?.selectionEnd ?? nextText.length,
    };
    pendingKeyRef.current = null;
    historyPosRef.current = null;
    draftRef.current = nextText;
    visualAnchorRef.current = selectionRef.current.start;
    visualHeadRef.current = selectionRef.current.start;
    restoreSelectionRef.current = true;
    setCaretPos(selectionRef.current.start);
  }, [stateKey]);

  useEffect(() => {
    const start = Math.max(0, Math.min(selectionRef.current.start, text.length));
    const end = Math.max(0, Math.min(selectionRef.current.end, text.length));
    setComposerState(stateKey, {
      text,
      vimMode,
      selectionStart: start,
      selectionEnd: end,
    });
  }, [caretPos, stateKey, text, vimMode]);

  useLayoutEffect(() => {
    if (!restoreSelectionRef.current) return;
    const el = inputRef.current;
    if (!el) return;
    const start = Math.max(0, Math.min(selectionRef.current.start, text.length));
    const end = Math.max(start, Math.min(selectionRef.current.end, text.length));
    el.setSelectionRange(start, end);
    setCaretPos(start);
    restoreSelectionRef.current = false;
  }, [stateKey, text]);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el || vimMode === "insert" || document.activeElement !== el) {
      setOverlayPos(null);
      return;
    }
    const at = Math.min(caretPos, text.length);
    const needsOverlay = text.length === 0 || at >= text.length || text[at] === "\n";
    if (!needsOverlay) {
      setOverlayPos(null);
      return;
    }
    setOverlayPos(measureCaret(el, at));
  }, [text, caretPos, vimMode]);

  const trigger = (() => {
    const el = inputRef.current;
    const caretAt = el && document.activeElement === el ? el.selectionStart : text.length;
    const match = SKILL_TRIGGER.exec(text.slice(0, caretAt ?? text.length));
    return match ? { sigil: match[1], partial: match[2], start: (caretAt ?? 0) - match[2].length - 1 } : null;
  })();
  const menuItems =
    trigger && vimMode === "insert" && !menuDismissed
      ? skills.filter((s) => s.name.startsWith(trigger.partial)).slice(0, 8)
      : [];

  function rememberSelection(start: number, end = start, caret = start) {
    selectionRef.current = { start, end };
    setCaretPos(caret);
  }

  function captureSelection(el: HTMLTextAreaElement) {
    const start = el.selectionStart ?? 0;
    const end = el.selectionEnd ?? start;
    rememberSelection(start, end, start);
  }

  function insertNewline() {
    const el = inputRef.current;
    const at = el ? el.selectionStart : text.length;
    setText((current) => current.slice(0, at) + "\n" + current.slice(at));
    requestAnimationFrame(() => {
      inputRef.current?.setSelectionRange(at + 1, at + 1);
      rememberSelection(at + 1);
    });
  }

  function acceptSkill(name: string) {
    if (!trigger) return;
    const insert = trigger.sigil === "$" ? `$${name}` : `/${name}`;
    const before = text.slice(0, trigger.start);
    const after = text.slice(trigger.start + 1 + trigger.partial.length);
    const next = `${before}${insert} ${after}`;
    setText(next);
    setMenuIndex(0);
    const pos = before.length + insert.length + 1;
    requestAnimationFrame(() => {
      inputRef.current?.setSelectionRange(pos, pos);
      rememberSelection(pos);
    });
  }

  async function attachFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) continue;
      const base64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve((reader.result as string).split(",", 2)[1] ?? "");
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      try {
        const uploaded = await uploadImage(file.type, base64);
        const token = `[image: ${uploaded.path}]`;
        const el = inputRef.current;
        const at = el ? el.selectionStart : text.length;
        setText((current) => {
          const pos = Math.min(at, current.length);
          const before = current.slice(0, pos);
          const after = current.slice(pos);
          const lead = before && !before.endsWith(" ") ? " " : "";
          const tail = after && !after.startsWith(" ") ? " " : "";
          return `${before}${lead}${token}${tail}${after}`;
        });
        setAttachments((current) => [...current, uploaded]);
      } catch {
        setError("Image upload failed");
      }
    }
  }

  const resizeComposer = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;
    if (!measureRef.current) measureRef.current = createTextareaMeasure();
    const surface = el.closest(".agent-session-surface, .session-side-panel") as HTMLElement | null;
    const avail = surface?.clientHeight ?? window.innerHeight;
    const cap = Math.max(80, Math.round(avail * 0.4));
    const contentHeight = measureTextareaHeight(el, measureRef.current);
    const desired = Math.min(contentHeight, cap);
    el.style.height = `${desired}px`;
    el.style.overflowY = contentHeight > cap ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    resizeComposer();
  }, [resizeComposer, text]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    const surface = el.closest(".agent-session-surface, .session-side-panel") as HTMLElement | null;
    const handleWindowResize = () => resizeComposer();
    window.addEventListener("resize", handleWindowResize);
    const observer = surface ? new ResizeObserver(() => resizeComposer()) : null;
    if (observer && surface) observer.observe(surface);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", handleWindowResize);
    };
  }, [resizeComposer]);

  useEffect(() => () => {
    measureRef.current?.remove();
    measureRef.current = null;
  }, []);

  function caret(): number {
    return inputRef.current?.selectionStart ?? 0;
  }

  // Block cursor: normal mode keeps a 1-char selection styled via ::selection.
  function setBlock(at: number) {
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      const start = Math.max(0, Math.min(at, Math.max(0, el.value.length - 1)));
      const end = Math.min(start + 1, el.value.length);
      el.setSelectionRange(start, end);
      rememberSelection(start, end, start);
    });
  }

  function setInsertCaret(at: number) {
    requestAnimationFrame(() => {
      inputRef.current?.setSelectionRange(at, at);
      rememberSelection(at);
    });
  }

  function enterInsert(at: number) {
    setVimMode("insert");
    setInsertCaret(at);
  }

  function focusPaneScope() {
    pendingKeyRef.current = null;
    setVimMode("pane");
    inputRef.current?.closest<HTMLDivElement>(".pane-frame")?.focus();
  }

  function motionTarget(key: string, at: number): number | null {
    switch (key) {
      case "h": return Math.max(0, at - 1);
      case "l": return Math.min(text.length, at + 1);
      case "0": return lineBounds(text, at).start;
      case "^": {
        const { start, end } = lineBounds(text, at);
        const match = text.slice(start, end).match(/\S/);
        return start + (match?.index ?? 0);
      }
      case "$": return lineBounds(text, at).end;
      case "w": return wordRight(text, at);
      case "e": return wordEnd(text, at) + 1;
      case "b": return wordLeft(text, at);
      default: return null;
    }
  }

  function applyOperator(op: string, from: number, to: number) {
    const [lo, hi] = from <= to ? [from, to] : [to, from];
    const chunk = text.slice(lo, hi);
    if (chunk) registerRef.current = chunk;
    if (op === "y") {
      void navigator.clipboard?.writeText(chunk).catch(() => {});
      setBlock(lo);
      return;
    }
    setText(text.slice(0, lo) + text.slice(hi));
    if (op === "c") enterInsert(lo);
    else setBlock(lo);
  }

  function handleNormalKey(event: React.KeyboardEvent<HTMLTextAreaElement>): void {
    const key = event.key;
    if (["Shift", "Control", "Alt", "Meta"].includes(key)) return;
    if (key === "r" && event.ctrlKey) {
      event.preventDefault();
      document.execCommand("redo");
      return;
    }
    event.preventDefault();
    const at = caret();
    const pending = pendingKeyRef.current;

    if (pending === "r") {
      pendingKeyRef.current = null;
      if (key.length === 1) {
        setText(text.slice(0, at) + key + text.slice(at + 1));
        setBlock(at);
      }
      return;
    }
    if (pending === "g") {
      pendingKeyRef.current = null;
      if (key === "g") setBlock(0);
      return;
    }
    if (pending === "d" || pending === "c" || pending === "y") {
      pendingKeyRef.current = null;
      if (key === pending) {
        // dd / cc / yy — whole line
        const { start, end } = lineBounds(text, at);
        applyOperator(pending, start, Math.min(end + 1, text.length));
        return;
      }
      const target = motionTarget(key, at);
      if (target !== null) applyOperator(pending, at, target);
      return;
    }

    const motion = motionTarget(key, at);
    if (motion !== null) {
      setBlock(key === "$" ? motion - 1 : motion);
      return;
    }
    switch (key) {
      case "Escape":
        focusPaneScope();
        break;
      case "G": setBlock(text.length - 1); break;
      case "g": pendingKeyRef.current = "g"; break;
      case "x":
        if (at < text.length) {
          registerRef.current = text[at];
          setText(text.slice(0, at) + text.slice(at + 1));
          setBlock(at);
        }
        break;
      case "X":
        if (at > 0) {
          registerRef.current = text[at - 1];
          setText(text.slice(0, at - 1) + text.slice(at));
          setBlock(at - 1);
        }
        break;
      case "s":
        setText(text.slice(0, at) + text.slice(at + 1));
        enterInsert(at);
        break;
      case "S":
        setText("");
        enterInsert(0);
        break;
      case "D": applyOperator("d", at, lineBounds(text, at).end); break;
      case "C": applyOperator("c", at, lineBounds(text, at).end); break;
      case "d": pendingKeyRef.current = "d"; break;
      case "c": pendingKeyRef.current = "c"; break;
      case "y": pendingKeyRef.current = "y"; break;
      case "r": pendingKeyRef.current = "r"; break;
      case "p": {
        const reg = registerRef.current;
        if (reg) {
          const pos = Math.min(text.length, at + 1);
          setText(text.slice(0, pos) + reg + text.slice(pos));
          setBlock(pos + reg.length - 1);
        }
        break;
      }
      case "P": {
        const reg = registerRef.current;
        if (reg) {
          setText(text.slice(0, at) + reg + text.slice(at));
          setBlock(at + reg.length - 1);
        }
        break;
      }
      case "u": document.execCommand("undo"); break;
      case "k": {
        // vim line-up unless the cursor sits on the first char — then message history
        if (at > 0 && text.length > 0) {
          setBlock(lineMove(text, at, -1));
          break;
        }
        if (history.length === 0) break;
        if (historyPosRef.current === null) {
          draftRef.current = text;
          historyPosRef.current = history.length - 1;
        } else if (historyPosRef.current > 0) {
          historyPosRef.current -= 1;
        }
        setText(history[historyPosRef.current]);
        setBlock(0);
        break;
      }
      case "j": {
        // vim line-down unless the cursor sits on the last char — then history forward
        if (text.length > 0 && at < text.length - 1) {
          setBlock(lineMove(text, at, 1));
          break;
        }
        if (historyPosRef.current === null) break;
        historyPosRef.current += 1;
        if (historyPosRef.current >= history.length) {
          historyPosRef.current = null;
          setText(draftRef.current);
          setBlock(0);
        } else {
          setText(history[historyPosRef.current]);
          setBlock(0);
        }
        break;
      }
      case "v":
        setVimMode("visual");
        visualAnchorRef.current = at;
        visualHeadRef.current = at;
        setVisualSelection(at, at);
        break;
      case "i": enterInsert(at); break;
      case "a": enterInsert(Math.min(text.length, at + 1)); break;
      case "I": enterInsert(lineBounds(text, at).start); break;
      case "A": enterInsert(lineBounds(text, at).end); break;
      case "o": {
        const { end } = lineBounds(text, at);
        setText(text.slice(0, end) + "\n" + text.slice(end));
        enterInsert(end + 1);
        break;
      }
      case "O": {
        const { start } = lineBounds(text, at);
        setText(text.slice(0, start) + "\n" + text.slice(start));
        enterInsert(start);
        break;
      }
      case "Enter": void send("now"); break;
      default: break;
    }
  }

  useEffect(() => {
    historyPosRef.current = null;
  }, [ticket]);

  function setVisualSelection(anchor: number, head: number) {
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      const lo = Math.min(anchor, head);
      const hi = Math.min(el.value.length, Math.max(anchor, head) + 1);
      el.setSelectionRange(lo, hi);
      rememberSelection(lo, hi, head);
    });
  }

  function handleVisualKey(event: React.KeyboardEvent<HTMLTextAreaElement>): void {
    const key = event.key;
    if (["Shift", "Control", "Alt", "Meta"].includes(key)) return;
    event.preventDefault();
    const anchor = visualAnchorRef.current;
    const head = visualHeadRef.current;

    const moveTo = (next: number) => {
      const clamped = Math.max(0, Math.min(next, Math.max(0, text.length - 1)));
      visualHeadRef.current = clamped;
      setVisualSelection(anchor, clamped);
    };

    const motion = motionTarget(key, head);
    if (motion !== null) {
      moveTo(key === "$" || key === "w" || key === "e" ? motion - (key === "$" ? 1 : key === "e" ? 1 : 0) : motion);
      return;
    }
    const lo = Math.min(anchor, head);
    const hi = Math.min(text.length, Math.max(anchor, head) + 1);
    switch (key) {
      case "j": moveTo(lineMove(text, head, 1)); break;
      case "k": moveTo(lineMove(text, head, -1)); break;
      case "G": moveTo(text.length - 1); break;
      case "o":
        visualAnchorRef.current = head;
        visualHeadRef.current = anchor;
        setVisualSelection(head, anchor);
        break;
      case "d":
      case "x": {
        registerRef.current = text.slice(lo, hi);
        setText(text.slice(0, lo) + text.slice(hi));
        setVimMode("normal");
        setBlock(lo);
        break;
      }
      case "c": {
        registerRef.current = text.slice(lo, hi);
        setText(text.slice(0, lo) + text.slice(hi));
        enterInsert(lo);
        break;
      }
      case "y": {
        registerRef.current = text.slice(lo, hi);
        void navigator.clipboard?.writeText(registerRef.current).catch(() => {});
        setVimMode("normal");
        setBlock(lo);
        break;
      }
      case "Escape":
      case "v":
        setVimMode("normal");
        setBlock(head);
        break;
      default:
        break;
    }
  }

  async function send(mode: "now" | "on-idle") {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await sendAgentMessage(ticket, value, mode);
      historyPosRef.current = null;
      setText("");
      setAttachments([]);
      if (mode === "on-idle" && result.messages) replaceTranscriptQueue(ticket, result.messages);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setBusy(false);
    }
  }

  async function cancel(index: number) {
    try {
      const result = await cancelQueuedMessage(ticket, index);
      replaceTranscriptQueue(ticket, result.messages);
    } catch {
      /* queue changed under us — the next session refresh will reconcile it */
    }
  }

  return (
    <div className="session-composer">
      {queued.map((message, i) => (
        <div className="session-queued" key={`${message.queued_at}-${i}`}>
          <Hourglass size={11} />
          <span className="session-queued-text">{message.text}</span>
          <button aria-label="Cancel queued message" type="button" onClick={() => cancel(i)}>
            <X size={12} />
          </button>
        </div>
      ))}
      {menuItems.length > 0 ? (
        <div className="session-skill-menu">
          {menuItems.map((skill, i) => (
            <button
              className={`session-skill-item${i === menuIndex ? " is-active" : ""}`}
              key={skill.name}
              type="button"
              onMouseDown={(event) => {
                event.preventDefault();
                acceptSkill(skill.name);
              }}
            >
              <span className="session-skill-name">
                {trigger?.sigil === "$" ? "$" : "/"}
                {skill.name}
              </span>
              {skill.description ? (
                <span className="session-skill-desc">{skill.description}</span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
      {error ? <div className="session-composer-error">{error}</div> : null}
      {attachments.length > 0 ? (
        <div className="session-attachments">
          {attachments.map((attachment, i) => (
            <div className="session-attachment" key={attachment.url}>
              <img alt="attachment" src={attachment.url} />
              <button
                aria-label="Remove attachment"
                type="button"
                onClick={() => {
                  setText((current) =>
                    current.replace(`[image: ${attachment.path}]`, "").replace(/  +/g, " ")
                  );
                  setAttachments((current) => current.filter((_, at) => at !== i));
                }}
              >
                <X size={11} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="session-composer-row">
        <div className="session-input-wrap">
        {overlayPos ? (
          <span
            className="session-empty-block-cursor"
            style={{ top: overlayPos.top, left: overlayPos.left }}
          />
        ) : null}
        <textarea
          autoCapitalize="off"
          autoCorrect="off"
          className={vimMode === "normal" || vimMode === "visual" ? "is-vim-normal" : undefined}
          spellCheck={false}
          placeholder={vimMode === "insert" ? "Enter sends now · Shift+Enter queues until idle · Esc = vim normal" : undefined}
          ref={inputRef}
          rows={2}
          value={text}
          onFocus={(event) => {
            if (vimMode !== "insert") enterInsert(event.currentTarget.selectionEnd ?? text.length);
            else captureSelection(event.currentTarget);
          }}
          onChange={(event) => {
            setText(event.target.value);
            setMenuDismissed(false);
            setMenuIndex(0);
            captureSelection(event.currentTarget);
          }}
          onClick={(event) => captureSelection(event.currentTarget)}
          onKeyUp={(event) => captureSelection(event.currentTarget)}
          onSelect={(event) => captureSelection(event.currentTarget)}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            if (event.dataTransfer.files.length > 0) {
              event.preventDefault();
              void attachFiles(event.dataTransfer.files);
            }
          }}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.items)
              .filter((item) => item.kind === "file")
              .map((item) => item.getAsFile())
              .filter((file): file is File => file !== null);
            if (files.length > 0) {
              event.preventDefault();
              void attachFiles(files);
            }
          }}
          onKeyDown={(event) => {
            if (vimMode === "normal") {
              handleNormalKey(event);
              return;
            }
            if (vimMode === "visual") {
              handleVisualKey(event);
              return;
            }
            if (menuItems.length > 0) {
              if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "j")) {
                event.preventDefault();
                setMenuIndex((i) => (i + 1) % menuItems.length);
                return;
              }
              if (
                event.key === "ArrowUp" ||
                (event.ctrlKey && event.key === "k") ||
                (event.key === "Tab" && event.shiftKey)
              ) {
                event.preventDefault();
                setMenuIndex((i) => (i - 1 + menuItems.length) % menuItems.length);
                return;
              }
              if (event.key === "Enter" || (event.key === "Tab" && !event.shiftKey)) {
                event.preventDefault();
                acceptSkill(menuItems[Math.min(menuIndex, menuItems.length - 1)].name);
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setMenuDismissed(true);
                return;
              }
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setVimMode("normal");
              setBlock(caret() - 1);
              return;
            }
            if (event.key === "j" && event.ctrlKey) {
              event.preventDefault();
              insertNewline();
              return;
            }
            if (event.key !== "Enter") return;
            if (event.shiftKey && !thinking) {
              // agent idle — queueing is pointless, give a newline instead
              event.preventDefault();
              insertNewline();
              return;
            }
            event.preventDefault();
            void send(event.shiftKey ? "on-idle" : "now");
          }}
        />
        </div>
        <button
          className="session-send"
          disabled={busy || !text.trim()}
          title="Send now"
          type="button"
          onClick={() => void send("now")}
        >
          <SendHorizontal size={14} />
        </button>
      </div>
      <div className="session-composer-status">
        {thinking ? <span className="session-thinking-indicator">thinking</span> : null}
        <div className="session-subagents">
          {runningSubagents.map((entry) => (
            <button
              className="session-subagent-chip"
              disabled={!onInspect}
              key={entry.id}
              title={entry.head}
              type="button"
              onClick={() => onInspect?.(entry.id)}
            >
              <span className="session-tool-running" />
              subagent {entry.id.slice(0, 8)}
            </button>
          ))}
        </div>
        <span className={`session-vim-mode is-${vimMode}`}>
          {vimMode === "insert"
            ? "-- INSERT --"
            : vimMode === "visual"
              ? "-- VISUAL --"
              : vimMode === "pane"
                ? "-- PANE --"
                : "-- NORMAL --"}
        </span>
      </div>
    </div>
  );
}

const WIDTH_KEY = "wiki-session-sidebar-width";
const MIN_WIDTH = 320;

function clampWidth(width: number): number {
  return Math.min(Math.max(width, MIN_WIDTH), Math.round(window.innerWidth * 0.7));
}

export type SidebarTarget = {
  ticket: string;
  kind: string | null;
  role: string | null;
  model?: string | null;
  pr?: string | null;
  canReview?: boolean;
};

export function SessionSidebar({
  worker,
  onClose,
  onOpenAgent,
}: {
  worker: SidebarTarget;
  onClose: () => void;
  onOpenAgent: (ticket: string, panel?: "review") => void;
}) {
  const [width, setWidth] = useState(() =>
    clampWidth(Number(localStorage.getItem(WIDTH_KEY)) || 480)
  );

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* synthetic events lack a real pointer — move/up listeners still work */
    }
    const onMove = (move: PointerEvent) => {
      setWidth(clampWidth(window.innerWidth - move.clientX));
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      setWidth((current) => {
        localStorage.setItem(WIDTH_KEY, String(current));
        return current;
      });
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  };

  return (
    <aside className="session-sidebar" style={{ width }}>
      <div className="session-resize" onPointerDown={startResize} />
      <div className="session-sidebar-inner">
        <header className="session-header">
          <span className="session-ticket">{worker.ticket}</span>
          {worker.kind ? <span className="agent-chip">{worker.kind}</span> : null}
          {worker.role ? <span className="agent-chip">{worker.role}</span> : null}
          {worker.model ? <span className="agent-chip is-faint">{worker.model}</span> : null}
          <div className="agent-surface-actions">
            <button
              className="agent-surface-action"
              type="button"
              onClick={() => onOpenAgent(worker.ticket)}
            >
              <ScrollText size={13} />
              Open
            </button>
            {worker.canReview ? (
              <button
                className="agent-surface-action"
                type="button"
                onClick={() => onOpenAgent(worker.ticket, "review")}
              >
                <GitPullRequest size={13} />
                Review
              </button>
            ) : null}
          </div>
          <button className="session-close" type="button" onClick={onClose} aria-label="Close">
            <X size={14} />
          </button>
        </header>
        <SessionTab showComposer={false} ticket={worker.ticket} />
      </div>
    </aside>
  );
}
