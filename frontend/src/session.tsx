import {
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ComponentProps, CSSProperties } from "react";
import {
  AlertTriangle,
  Bell,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleDashed,
  CircleSlash,
  GitPullRequest,
  Hourglass,
  ListTodo,
  MessageCircleQuestion,
  Radio,
  RefreshCw,
  ScrollText,
  SendHorizontal,
  SlashSquare,
  Wrench,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { MarkdownPre, MarkdownTable, prepareTranscriptMarkdown, rehypeEscapeRawHtml } from "./markdown";
import {
  cancelAgentModelChange,
  cancelQueuedMessage,
  getAgentModels,
  getSkills,
  getSubagentSession,
  respondToAgentRequest,
  sendAgentMessage,
  setAgentModel,
  uploadImage,
} from "./api";
import type {
  AgentModelOption,
  AgentSessionData,
  ComposerMessage,
  ProviderEventInspector,
  ProviderPendingRequest,
  QueuedMessage,
  SessionEvent,
  SessionInit,
  SessionPatch,
  SessionRateLimit,
  SessionPr,
  SessionTool,
  SkillInfo,
  SubagentInfo,
} from "./api";
import { hasAnsi, renderAnsi } from "./ansi";
import { ArtifactBlock } from "./artifact-block";
import { useArtifactInspector } from "./artifact-inspector";
import { SplitDiffView } from "./split-diff";
import {
  filterCommands,
  initialValues,
  missingRequired,
  serializeCommand,
  type ComposerCommand,
} from "./composer-commands";
import {
  CommandForm,
  SLASH_MENU_ID,
  SlashMenu,
  slashMenuOptionId,
} from "./composer-slash-menu";
import { externalLinkProps } from "./external-links";
import { useModalA11y } from "./modal-a11y";
import {
  GhPreviewCard,
  GhPreviewInline,
  GhPreviewLayoutContext,
  containsGitHubPreviewUrl,
  isGitHubPreviewUrl,
  renderAnsiWithGitHubLinks,
} from "./github-preview";
import { LoadingPlaceholder } from "./loading";
import { createStateKeyWriteBarrier, deletePaneStateEntries } from "./pane-state-cache";
import { Timestamp } from "./timestamp";
import { StatusBadge } from "./status-badge";
import { BoundedPreview } from "./transcript-preview";
import {
  COLLAPSE_HEIGHT_PX,
  bashReadTargetPath,
  wouldRenderTall,
  compactJsonDetail,
  detectStructuredContent,
  formatEventDuration,
  isBashTool,
  isReadOrSearchTool,
  parseEmbeddedScripts,
  thoughtDurations,
  toolDiffIsTruncated,
  toolGlyph,
  toolInlineResult,
  toolDiffSource,
  toolOutputPeek,
  toolPathHint,
  toolPresentation,
  toolStatus,
  toolSummaryLine,
  toolSummaryParts,
  traceRows,
  turnMetaDurations,
  type ToolPresentation,
  type TraceRow,
} from "./transcript-event-utils";
import { HighlightedCode, NumberedReadHighlight, ShikiCode, languageForPath, languageFromContent } from "./shiki";
import { parseNumberedPayload, type NumberedPayload } from "./read-gutter";
import {
  HarnessOutput,
  clipSegmentsInline,
  parseHarnessOutput,
  segmentsForDisplayText,
} from "./transcript-output";
import type { HarnessOutputSegment } from "./transcript-output";
import { CodexStreamHighlights } from "./codex-stream-renderers";
import { markerRule } from "./hook-message-registry";
import type { MarkerSeverity } from "./hook-message-registry";
import {
  activityRunStateFromProvider,
  activityStateLabel,
  type ActivityRunState,
} from "./agent-events";
import {
  addPendingUserMessage,
  composerTextMatches,
  invalidateTranscript,
  clearInlineArtifactStates,
  loadOlderEvents,
  removePendingUserMessage,
  refreshTranscript,
  replaceTranscriptDesiredModel,
  replaceTranscriptQueue,
  retryPendingUserMessage,
  retryTranscript,
  updatePendingUserMessage,
  useTranscriptSession,
  type PendingUserMessage,
  type TranscriptSession,
} from "./transcript-store";
import {
  buildVirtualLayoutIncremental,
  eventRowsIncremental,
  sameEventRefs,
  type EventRow,
  type EventRowsCache,
  type RowMeasurement,
  type VirtualLayout,
  type VirtualLayoutCache,
} from "./session-layout";

const POLL_MS = 2500;
const VIRTUAL_MIN_OVERSCAN = 3600;
const VIRTUAL_OVERSCAN_MULTIPLIER = 5;
const VIRTUAL_DEFAULT_VIEWPORT = 720;
const COMPOSER_MIN_HEIGHT = 44;
const COMPOSER_NARROW_WIDTH = 480;

type ComposerShortcut = {
  key: string;
  action: string;
};

type ComposerGuidance = {
  shortcuts: ComposerShortcut[];
  accessibleText: string;
  sendAction: string;
};

function getComposerGuidance(working: boolean): ComposerGuidance {
  const shortcuts = working
    ? [
        { key: "enter", action: "send now" },
        { key: "shift+enter", action: "queue until idle" },
        { key: "esc", action: "vim" },
      ]
    : [
        { key: "enter", action: "send" },
        { key: "shift+enter", action: "newline" },
        { key: "esc", action: "vim" },
      ];
  return {
    shortcuts,
    accessibleText: shortcuts.map(({ key, action }) => `${key} ${action}`).join(" · "),
    sendAction: working ? "Send now" : "Send",
  };
}

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

function useElementVisible(node: HTMLElement | null): boolean {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!node) {
      setVisible(false);
      return;
    }

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

    const intersectionObserver = new IntersectionObserver(() => compute());
    const resizeObserver = new ResizeObserver(() => compute());
    intersectionObserver.observe(node);
    resizeObserver.observe(node);
    document.addEventListener("visibilitychange", compute);
    window.addEventListener("focus", compute);
    window.addEventListener("resize", compute);
    window.addEventListener("scroll", compute, true);
    compute();
    return () => {
      intersectionObserver.disconnect();
      resizeObserver.disconnect();
      document.removeEventListener("visibilitychange", compute);
      window.removeEventListener("focus", compute);
      window.removeEventListener("resize", compute);
      window.removeEventListener("scroll", compute, true);
    };
  }, [node]);

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
          <span>Advanced response</span>
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
        <button disabled={sending || sent} type="button" onClick={() => void submit()}>
          {sent
            ? "Response sent"
            : sending
              ? "Sending…"
              : questions.length
                ? "Send answers"
                : "Send response"}
        </button>
      </div>
      {/* R1-04/WIKI-235: request kind, id, and raw payload do not render in
          default session chrome. They remain available through event APIs. */}
      {error ? <div className="session-provider-request-error">{error}</div> : null}
    </div>
  );
}

type QuestionAnswerDraft = {
  optionIndexes: number[];
  otherSelected: boolean;
  customReply: string;
};

type QuestionDraft = {
  answers: Record<string, QuestionAnswerDraft>;
  sending: boolean;
  submitted: boolean;
  error: string | null;
};

type QuestionUiContextValue = {
  drafts: Record<string, QuestionDraft>;
  groups: Map<string, SessionEvent[]>;
  selectOption: (event: SessionEvent, optionIndex: number) => void;
  selectOther: (event: SessionEvent, selected: boolean) => void;
  setCustomReply: (event: SessionEvent, value: string) => void;
  submitQuestions: (event: SessionEvent) => void;
};

const QuestionUiContext = createContext<QuestionUiContextValue | null>(null);

function emptyQuestionAnswer(): QuestionAnswerDraft {
  return { optionIndexes: [], otherSelected: false, customReply: "" };
}

function questionIsResolved(question: NonNullable<SessionEvent["question"]>): boolean {
  return (
    question.answered_option !== null ||
    (question.answered_options ?? []).length > 0 ||
    Boolean(question.custom_reply)
  );
}

function resolvedQuestionAnswer(
  question: NonNullable<SessionEvent["question"]>,
): string | string[] | null {
  const optionIndexes = question.multi_select
    ? question.answered_options ?? []
    : question.answered_option !== null
      ? [question.answered_option]
      : question.answered_options ?? [];
  const values = optionIndexes
    .map((index) => question.options[index])
    .filter((value): value is string => Boolean(value));
  if (question.custom_reply) values.push(question.custom_reply);
  if (question.multi_select) return values.length ? values : null;
  return values[0] ?? null;
}

function draftQuestionAnswer(
  question: NonNullable<SessionEvent["question"]>,
  answer: QuestionAnswerDraft | undefined,
): string | string[] | null {
  if (!answer) return null;
  const values = answer.optionIndexes
    .map((index) => question.options[index])
    .filter((value): value is string => Boolean(value));
  const customReply = answer.customReply.trim();
  if (answer.otherSelected) {
    if (!customReply) return null;
    values.push(customReply);
  }
  if (question.multi_select) return values.length ? values : null;
  return values.length === 1 ? values[0] : null;
}

function questionGroupPayload(
  group: SessionEvent[],
  answers: Record<string, QuestionAnswerDraft>,
): Record<string, string | string[]> | null {
  const payload: Record<string, string | string[]> = {};
  for (const event of group) {
    const question = event.question;
    if (!question) continue;
    const answer = questionIsResolved(question)
      ? resolvedQuestionAnswer(question)
      : draftQuestionAnswer(question, answers[question.prompt]);
    if (answer === null) return null;
    payload[question.prompt] = answer;
  }
  return Object.keys(payload).length ? payload : null;
}

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

function correlateComposerMessages(
  events: SessionEvent[],
  composerMessages: ComposerMessage[],
): { claimed: Map<number, string | null>; unmatched: ComposerMessage[] } {
  // Reserve real transcript user events for composer messages FIFO. Every
  // composer message competes for a slot — unsourced (Henry) rows reserve
  // before sourced rows can claim, so an identical human turn within a
  // synthetic turn's 2s window can never be mislabelled as synthetic.
  const claimed = new Map<number, string | null>();
  const unmatched: ComposerMessage[] = [];
  const byPendingId = new Map<string, number>();
  events.forEach((event, index) => {
    if (event.kind !== "user") return;
    const pendingId = event.pending_id;
    if (pendingId && !byPendingId.has(pendingId)) {
      byPendingId.set(pendingId, index);
    }
  });
  for (const message of composerMessages) {
    // Canonical mapping: durable pending_id is unambiguous when present.
    const durableIndex = byPendingId.get(message.pending_id);
    if (durableIndex !== undefined && !claimed.has(durableIndex)) {
      claimed.set(durableIndex, message.source ?? null);
      continue;
    }
    // Text fallback for providers that do not echo pending_id (live claude
    // sessions parse the raw transcript, which never carries one). Scan FIFO
    // so an earlier unsourced composer message reserves its Henry-bubble slot
    // before a later synthetic one is allowed to take it. The match window
    // spans BOTH sent_at and echoed_at: the transcript event lands at
    // ~sent_at while echo detection can lag by several seconds, so anchoring
    // on echoed_at alone pushed real events outside the old 2s lookback and
    // duplicated the message. The window stays bounded on both sides because
    // composer_messages is unbounded across run replacement while the
    // transcript window is trimmed — an old sourced row whose real event has
    // fallen out must not claim a later identical terminal-typed Henry row.
    const sentAt = message.sent_at ? Date.parse(message.sent_at) : Number.NaN;
    const echoedAt = message.echoed_at ? Date.parse(message.echoed_at) : Number.NaN;
    const anchorTimes = [sentAt, echoedAt].filter((value) => Number.isFinite(value));
    const anchorLow = anchorTimes.length ? Math.min(...anchorTimes) : Number.NaN;
    const anchorHigh = anchorTimes.length ? Math.max(...anchorTimes) : Number.NaN;
    const FALLBACK_LOOKBACK_MS = 2_000;
    const FALLBACK_LOOKAHEAD_MS = 60_000;
    const matchIndex = events.findIndex((event, index) => {
      if (claimed.has(index) || event.kind !== "user") return false;
      if (event.pending_id) return false;
      const eventAt = event.ts ? Date.parse(event.ts) : Number.NaN;
      if (Number.isFinite(anchorLow) && Number.isFinite(eventAt)) {
        if (eventAt < anchorLow - FALLBACK_LOOKBACK_MS) return false;
        if (eventAt > anchorHigh + FALLBACK_LOOKAHEAD_MS) return false;
      } else if (Number.isFinite(anchorLow) !== Number.isFinite(eventAt)) {
        // Composer has a durable anchor but the event does not (or vice
        // versa): without both timestamps the window guard is meaningless,
        // so refuse the fallback rather than allow an unbounded match.
        return false;
      }
      return composerTextMatches(event.text, message.text);
    });
    if (matchIndex >= 0) {
      claimed.set(matchIndex, message.source ?? null);
      continue;
    }
    unmatched.push(message);
  }
  return { claimed, unmatched };
}

function composerMessageEvents(session: TranscriptSession): SessionEvent[] {
  const { unmatched } = correlateComposerMessages(
    session.events,
    session.composerMessages,
  );
  return unmatched.map((message) => ({
    id: 2_000_000 + message.seq,
    kind: "user" as const,
    // sent_at orders the fallback row where the message actually entered the
    // conversation; echoed_at can lag several seconds behind ("in a moment").
    ts: message.sent_at ?? message.echoed_at,
    text: message.text,
    disposition: "rendered" as const,
    source: message.source ?? null,
    pending_id: message.pending_id,
  }));
}

function applyComposerSources(
  events: SessionEvent[],
  composerMessages: ComposerMessage[],
): SessionEvent[] {
  if (composerMessages.length === 0) return events;
  const { claimed } = correlateComposerMessages(events, composerMessages);
  let mutated = false;
  const next = events.map((event, index) => {
    if (!claimed.has(index)) return event;
    const source = claimed.get(index);
    if (!source) return event;
    if (event.source === source) return event;
    mutated = true;
    return { ...event, source };
  });
  return mutated ? next : events;
}

function mergeComposerEvents(events: SessionEvent[], composerEvents: SessionEvent[]): SessionEvent[] {
  if (composerEvents.length === 0) return events;
  const merged = events.slice();
  for (const event of composerEvents) {
    const eventTs = event.ts ? Date.parse(event.ts) : Number.NaN;
    const index = Number.isFinite(eventTs)
      ? merged.findIndex((candidate) => {
        const candidateTs = candidate.ts ? Date.parse(candidate.ts) : Number.NaN;
        return Number.isFinite(candidateTs) && candidateTs > eventTs;
      })
      : -1;
    if (index >= 0) merged.splice(index, 0, event);
    else merged.push(event);
  }
  return merged;
}

export function SessionModelFooter({
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
  const modelTriggerRef = useRef<HTMLButtonElement | null>(null);
  const modelA11yId = useId();
  const modelMenuId = `session-model-menu-${modelA11yId}`;
  const modelConfirmTitleId = `session-model-confirm-title-${modelA11yId}`;
  const modelConfirmRef = useModalA11y<HTMLDivElement>(
    Boolean(confirmModel),
    () => setConfirmModel(null),
    modelTriggerRef,
  );
  const modelMenuRef = useRef<HTMLDivElement | null>(null);
  const modelOptionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [menuActiveIndex, setMenuActiveIndex] = useState(0);
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
    if (!open || loading || allowedModels.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      setMenuActiveIndex(0);
      modelOptionRefs.current[0]?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [allowedModels.length, loading, open]);

  function handleModelMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      modelTriggerRef.current?.focus();
      return;
    }
    const options = modelOptionRefs.current.filter(
      (option): option is HTMLButtonElement => option !== null,
    );
    if (options.length === 0) return;
    const activeIndex = options.indexOf(document.activeElement as HTMLButtonElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = activeIndex < 0 ? 0 : (activeIndex + 1) % options.length;
    else if (event.key === "ArrowUp") nextIndex = activeIndex < 0 ? options.length - 1 : (activeIndex - 1 + options.length) % options.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = options.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    setMenuActiveIndex(nextIndex);
    options[nextIndex]?.focus();
  }

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
      <span className="session-footer-kind">{kind}</span>
      <span aria-hidden="true" className="session-footer-sep">·</span>
      <span className={`session-model-badge${desiredModel ? " has-queued" : ""}`}>
        <button
          aria-expanded={open}
          aria-controls={modelMenuId}
          aria-haspopup="menu"
          aria-label="Change model"
          className="session-model-current"
          disabled={busy}
          ref={modelTriggerRef}
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
        <div
          aria-label="Available models"
          className="session-model-menu"
          id={modelMenuId}
          ref={modelMenuRef}
          role="menu"
          onKeyDown={handleModelMenuKeyDown}
        >
          {loading ? <div className="session-model-empty">Loading models</div> : null}
          {!loading && allowedModels.length === 0 ? (
            <div className="session-model-empty">No alternate models</div>
          ) : null}
          {allowedModels.map((option, index) => (
            <button
              className="session-model-option"
              key={option.id}
              ref={(element) => { modelOptionRefs.current[index] = element; }}
              role="menuitem"
              tabIndex={index === menuActiveIndex ? 0 : -1}
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
        <div
          aria-labelledby={modelConfirmTitleId}
          className="session-model-confirm"
          ref={modelConfirmRef}
          role="dialog"
          aria-modal="true"
          tabIndex={-1}
        >
          <div className="session-model-confirm-text">
            <strong id={modelConfirmTitleId} className="sr-only">Confirm model switch</strong>
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

export function ProviderActionRequired({
  inspector,
  ticket,
}: {
  inspector: ProviderEventInspector;
  ticket: string;
}) {
  const pendingRequests = inspector.pending_requests ?? [];
  if (pendingRequests.length === 0) return null;
  return (
    <div className="session-action-required" data-testid="session-action-required">
      <div className="session-action-required-head">
        <AlertTriangle size={13} />
        <span className="session-action-required-title">Action required</span>
        {pendingRequests.length > 1 ? (
          <span className="session-action-required-count">{pendingRequests.length}</span>
        ) : null}
      </div>
      <div className="session-action-required-body">
        {pendingRequests.map((request) => (
          <ProviderPendingRequestCard
            key={`${typeof request.request_id}:${request.request_id}`}
            request={request}
            ticket={ticket}
          />
        ))}
      </div>
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

// Bash command titles render structured when the command embeds scripts
// (heredocs, -c/-e bodies): bash stays bash, embedded bodies highlight in
// their own language with a quiet indent. Ambiguous commands parse to null
// and keep the flat bash render; the raw toggle always has exact bytes.
function CommandHighlight({ command }: { command: string }) {
  const segments = useMemo(() => parseEmbeddedScripts(command), [command]);
  if (!segments) return <HighlightedCode code={command} lang="bash" />;
  return (
    <>
      {segments.map((segment, index) => (
        segment.kind === "bash"
          ? <HighlightedCode code={segment.text} key={index} lang="bash" />
          : (
            <span
              className={`session-command-embed${segment.kind === "embed-inline" ? " is-inline" : ""}`}
              data-lang={segment.lang}
              key={index}
            >
              <HighlightedCode code={segment.text} lang={segment.lang} />
            </span>
          )
      ))}
    </>
  );
}

function toolInlineDetail(tool: SessionTool): string | null {
  if (!tool.input || toolPresentation(tool) !== "inline") return null;
  const input = tool.input.trim();
  if (!input || toolSummaryLine(tool).includes(input)) return null;
  return compactJsonDetail(input) ?? input;
}

function renderOutputSegments(
  segments: HarnessOutputSegment[],
  text: string,
  withGitHubLinks = false,
) {
  const visible = segmentsForDisplayText(segments, text);
  if (!withGitHubLinks) return <HarnessOutput segments={visible} ansi />;
  return visible.map((segment, index) => (
    <span key={`${segment.kind}:${index}`}>
      {index > 0 ? "\n" : null}
      <span className={`session-output-segment is-${segment.kind}`}>
        {renderAnsiWithGitHubLinks(segment.text)}
      </span>
    </span>
  ));
}

function toolBlockTitle(tool: SessionTool): string {
  const summary = toolSummaryLine(tool).trim();
  if (isBashTool(tool)) return `$ ${tool.input.split("\n")[0] || summary}`;
  return `← ${summary}`;
}

// WIKI-253: readers can explicitly override the default-collapse per event.
// State lives in the per-session UI store (SessionUiStateContext), so numeric
// event ids that recycle across sessions don't cross-contaminate, and the
// store is discarded when the session unmounts via clearSessionPaneState.
// Three-state: undefined (follow default), true (user expanded), false (user
// collapsed after auto-expand).
function useToolOutputExpanded(
  eventId: number,
  defaultCollapsed: boolean,
): [boolean, () => void] {
  const uiState = useContext(SessionUiStateContext);
  const key = `tool-output:${eventId}`;
  const [override, setOverride] = useState<boolean | undefined>(
    () => uiState?.overrides.get(key),
  );
  const expanded = override ?? !defaultCollapsed;
  const toggle = useCallback(() => {
    const next = !(override ?? !defaultCollapsed);
    if (uiState) {
      if (next === !defaultCollapsed) uiState.overrides.delete(key);
      else uiState.overrides.set(key, next);
    }
    setOverride(next === !defaultCollapsed ? undefined : next);
  }, [defaultCollapsed, key, override, uiState]);
  return [expanded, toggle];
}

// WIKI-274 size cap on the content-based language fallback: skip the shape
// scan for anything larger than a small file's worth of code. Keeps huge log
// dumps out of the tokenizer without changing the small-read behavior.
const READ_CONTENT_FALLBACK_MAX_BYTES = 64 * 1024;

// WIKI-253 render-layer gate: promote any tool-output block above the height
// budget to collapsed presentation, regardless of tool kind or sub-renderer.
// Measurement happens on completion via ResizeObserver — the block renders
// full first, offsetHeight is observed, and if it clears COLLAPSE_HEIGHT_PX
// the block snaps to a peek row on the next commit. Explicit user expansion
// survives the completion transition.
function useMeasuredHeight(node: HTMLElement | null): number | null {
  // Latch tallest-seen height in a ref so the value survives the child body
  // unmounting when we transition to collapsed — otherwise the ref-based
  // measurement resets, defaultCollapse flips back to false, the body
  // remounts, measurement re-fires, and we loop. Height is monotonic here by
  // design: once tall, always tall (streams only grow, static outputs settle).
  const maxSeenRef = useRef<number | null>(null);
  const [, bumpRender] = useState(0);
  useLayoutEffect(() => {
    if (!node) return;
    const measure = () => {
      const observed = node.offsetHeight;
      if (maxSeenRef.current === null || observed > maxSeenRef.current) {
        maxSeenRef.current = observed;
        bumpRender((version) => version + 1);
      }
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node]);
  return maxSeenRef.current;
}

function ToolOutputBody({
  displayOutput,
  diffSource,
  eventId,
  numberedRead,
  pathLang,
  presentation,
  rawOutput,
  segments,
  tool,
}: {
  displayOutput: string;
  diffSource: string | null;
  eventId: number;
  // WIKI-261: pre-parsed numbered read payload (null when the output is not a
  // read-tool numbered dump or when no filetype hint is available). When
  // present the block renders as a two-column gutter + highlighted code
  // instead of a plain ANSI segment stream.
  numberedRead: NumberedPayload | null;
  pathLang: string | null;
  // Effective presentation from the parent (which applies the WIKI-253
  // inline→block promotion for long generic tool outputs). Recomputing here
  // would miss the promotion and hide long read_agent / list_agents payloads.
  presentation: ToolPresentation;
  rawOutput: string;
  segments: HarnessOutputSegment[];
  tool: SessionTool;
}) {
  const running = tool.ok === null;
  const diffDegraded = toolDiffIsTruncated(tool);
  const bash = isBashTool(tool);
  const hasGitHubLink = containsGitHubPreviewUrl(displayOutput);
  // WIKI-253 error exclusion: error-tone output stays expanded regardless of
  // the tool.ok flag — some tools mark structured errors with ok:true (e.g. a
  // failure captured in a JSON payload). Tone comes from either the explicit
  // ok=false or a tagged error segment; the flag alone is too coarse.
  const outputTone = tool.ok === false || segments.some((segment) => segment.kind === "error")
    ? "error"
    : "normal";
  // Measured height gates collapse instead of line-count/char-count heuristics
  // (MED #1). The block renders full, ResizeObserver reports offsetHeight, and
  // if it clears COLLAPSE_HEIGHT_PX we snap to a peek row on the next commit.
  // Wrap and font-size are baked into the pixel measurement, so a 12-line
  // block of 200-char lines and 12 lines of 20 chars are distinguished.
  const [bodyNode, setBodyNode] = useState<HTMLDivElement | null>(null);
  const measuredHeight = useMeasuredHeight(bodyNode);
  const measuredTall = measuredHeight !== null && measuredHeight > COLLAPSE_HEIGHT_PX;
  // Exclusions: diffs, error-tone output, and live streams stay expanded.
  // The live-stream exclusion drops at the running→complete transition (HIGH
  // #3): the block collapses on completion the same way any tall output does,
  // unless the reader clicked expand mid-stream (override is preserved by
  // useToolOutputExpanded).
  const collapsible = presentation !== "diff" && outputTone !== "error" && !running;
  const defaultCollapse = collapsible && measuredTall;
  const [expanded, toggleExpanded] = useToolOutputExpanded(eventId, defaultCollapse);
  const showCollapsed = defaultCollapse && !expanded;

  if (presentation === "inline" || (!displayOutput && !bash)) return null;
  const failed = tool.ok === false;
  if (presentation === "diff") {
    return (
      <div className="session-tool-body session-tool-diff-body">
        <div className="session-tool-block-title">{toolBlockTitle(tool)}</div>
        {failed ? (
          <div className="session-tool-failure-output">
            {renderOutputSegments(segments, displayOutput || rawOutput)}
          </div>
        ) : diffDegraded ? (
          <div className="session-tool-degraded-diff">
            edit too large to diff — view raw
          </div>
        ) : (
          <SplitDiffView
            className="session-tool-split-diff"
            emptyClassName="diff-view-empty"
            emptyMessage="No diff to display."
            patch={diffSource ?? displayOutput}
            viewType="split"
          />
        )}
      </div>
    );
  }
  // WIKI-252: GitHub URLs inside tool output render as plain external-link
  // anchors (no PR-metadata card unfurl); highlighter branches yield to the
  // text path so those anchors stay clickable instead of getting swallowed
  // into a Shiki code block.
  // File-slice bash reads (sed -n over one .py file etc.) highlight their
  // OUTPUT in the target file's language; ANSI-decorated output keeps the
  // ansi path, everything ambiguous stays plain.
  const canHighlightOutput = outputTone === "normal" && !hasGitHubLink && !hasAnsi(displayOutput);
  const bashReadTarget = bash && canHighlightOutput
    ? bashReadTargetPath(tool.input)
    : null;
  const bashOutputLang = bashReadTarget ? languageForPath(bashReadTarget) : null;
  // WIKI-270: JSON tool outputs (agent tools AND bash — think `gh api graphql`)
  // read pretty-printed in the block view instead of a one-line escape wall.
  // `detectStructuredContent` guards on size and only commits on a successful
  // parse, so truncated / mixed / non-JSON payloads fall through to raw. ANSI-
  // decorated bash output and file-slice reads keep their existing renderers.
  const structured = outputTone === "normal" && !hasGitHubLink && !bashOutputLang && !(bash && hasAnsi(displayOutput))
    ? detectStructuredContent(displayOutput)
    : null;
  // WIKI-274: codex/claude read outputs (archetype === "read") never hit the
  // bash file-slice path — the language falls out of the file summary
  // (pathLang) or, when the summary carries no filename, from the content
  // itself. Same guards as bashOutputLang so ansi output, GitHub-anchor
  // preview text, JSON pretty-print (WIKI-270), and cat -n numbered reads
  // (WIKI-261) all keep their existing renderers unchanged.
  const canFallbackFromContent = canHighlightOutput && !bashOutputLang && !structured && !numberedRead;
  const readOutputLang = canFallbackFromContent
    ? (pathLang
      ?? (displayOutput.length <= READ_CONTENT_FALLBACK_MAX_BYTES
        ? languageFromContent(displayOutput)
        : null))
    : null;
  const titleNode = bash ? (
    <div className="session-tool-block-title is-command">
      $ <CommandHighlight command={tool.input || toolSummaryLine(tool)} />
    </div>
  ) : (
    <div className="session-tool-block-title">{toolBlockTitle(tool)}</div>
  );
  if (showCollapsed) {
    const peek = toolOutputPeek(tool, displayOutput || rawOutput);
    return (
      <div className="session-tool-body session-tool-block-body is-collapsed">
        {titleNode}
        <ToolOutputPeekRow peek={peek} onExpand={toggleExpanded} />
      </div>
    );
  }
  // WIKI-253: content flows full-height so ResizeObserver can measure it. The
  // old BoundedPreview clip (10/3 lines) is superseded by the collapse gate —
  // short outputs measure short and stay expanded, tall outputs collapse to a
  // one-line peek. The ref hangs on a stable wrapper the observer watches.
  return (
    <div className={`session-tool-body session-tool-block-body${expanded ? " is-expanded" : ""}`}>
      {titleNode}
      <div className="session-tool-block-preview-measure" ref={setBodyNode}>
        <BoundedPreview
          ansi
          className="session-tool-block-preview"
          expandable={false}
          previewLines={Number.POSITIVE_INFINITY}
          rawText={rawOutput}
          showSummary={false}
          text={structured?.text ?? displayOutput}
          tone={outputTone}
          variant="block"
          renderBody={({ text }) => (
            <div className="session-tool-output-blocks">
              <span className="session-tool-output-text">
                {numberedRead && pathLang && outputTone === "normal" && !hasGitHubLink
                  ? <NumberedReadHighlight payload={numberedRead} lang={pathLang} />
                  : structured
                  ? <HighlightedCode code={text} lang={structured.lang} />
                  : bashOutputLang
                  ? <HighlightedCode code={text} lang={bashOutputLang} />
                  : readOutputLang
                  ? <HighlightedCode code={text} lang={readOutputLang} />
                  : renderOutputSegments(segments, text, hasGitHubLink)}
              </span>
            </div>
          )}
        />
      </div>
      {defaultCollapse && expanded ? (
        <button
          aria-expanded="true"
          className="session-tool-output-collapse"
          type="button"
          onClick={toggleExpanded}
        >
          collapse output
        </button>
      ) : null}
    </div>
  );
}

function ToolOutputPeekRow({
  peek,
  onExpand,
}: {
  peek: ReturnType<typeof toolOutputPeek>;
  onExpand: () => void;
}) {
  const lineLabel = peek.lines === 1 ? "line" : "lines";
  return (
    <button
      aria-expanded="false"
      className="session-tool-output-peek"
      type="button"
      onClick={onExpand}
    >
      <span aria-hidden="true" className="session-tool-output-peek-chevron">›</span>
      <span className="session-tool-output-peek-preview">{peek.preview}</span>
      <span className="session-tool-output-peek-meta">
        {peek.lines} {lineLabel} · {peek.size}
      </span>
      <span className="session-tool-output-peek-hint">show output</span>
    </button>
  );
}

const SUBTRACE_MAX_ROWS = 60;
// Hard memory bound on retained child events — well above the rendered row
// window (rows pair calls with results), far below a full long session.
const CHILD_TRACE_MAX_EVENTS = 400;

type ChildTraceState = { cursor: number; events: SessionEvent[]; hasOlder: boolean };

// Virtualized rows unmount offscreen; the cache keeps the merged, bounded
// child state (with its poll cursor) so scrolling back neither blanks the
// rows nor refetches the transcript from scratch. LRU-capped: entries for
// long-gone agent tools are evicted instead of accumulating per session.
const SUBTRACE_CACHE_MAX = 12;
const subagentTraceCache = new Map<string, ChildTraceState>();

function readChildTraceCache(key: string): ChildTraceState | null {
  const state = subagentTraceCache.get(key);
  if (!state) return null;
  // Refresh recency so the insertion-ordered Map behaves as an LRU.
  subagentTraceCache.delete(key);
  subagentTraceCache.set(key, state);
  return state;
}

function writeChildTraceCache(key: string, state: ChildTraceState) {
  subagentTraceCache.delete(key);
  subagentTraceCache.set(key, state);
  while (subagentTraceCache.size > SUBTRACE_CACHE_MAX) {
    const oldest = subagentTraceCache.keys().next().value;
    if (oldest === undefined) break;
    subagentTraceCache.delete(oldest);
  }
}

function applyChildPatches(events: SessionEvent[], patches: SessionPatch[]): SessionEvent[] {
  if (!patches.length) return events;
  const byId = new Map(patches.map((patch) => [patch.id, patch]));
  let changed = false;
  const next = events.map((event) => {
    const patch = byId.get(event.id);
    if (!patch || !event.tool) return event;
    if (
      event.tool.output === patch.output
      && event.tool.ok === patch.ok
      && event.tool.call_id === patch.call_id
      && event.tool.partial === patch.partial
      && event.tool.completed_at === patch.completed_at
    ) return event;
    changed = true;
    return {
      ...event,
      tool: {
        ...event.tool,
        call_id: patch.call_id ?? event.tool.call_id,
        output: patch.output,
        ok: patch.ok,
        completed_at: patch.completed_at ?? event.tool.completed_at,
        partial: patch.partial ?? event.tool.partial,
      },
    };
  });
  return changed ? next : events;
}

// Cursor-delta merge for the child transcript: keep events below the server's
// tail window, append the delta, apply patches by absolute event id, then
// trim to the retention bound (patches for trimmed events no-op by id-match).
function mergeChildTraceDelta(state: ChildTraceState | null, data: AgentSessionData): ChildTraceState {
  let events: SessionEvent[];
  let hasOlder = Boolean(data.has_older);
  if (!state || data.cursor < state.cursor) {
    events = data.events;
  } else {
    events = state.events.filter((event) => event.id < data.tail_from).concat(data.events);
    hasOlder = state.hasOlder || hasOlder;
  }
  events = applyChildPatches(events, data.patches ?? []);
  if (events.length > CHILD_TRACE_MAX_EVENTS) {
    events = events.slice(events.length - CHILD_TRACE_MAX_EVENTS);
    hasOlder = true;
  }
  return { cursor: data.cursor, events, hasOlder };
}

// OpenCode-style sub-agent grouping: an agent tool (Task/Explore) renders its
// child trace — thinking and tool calls with their inputs/outputs — through
// the same always-visible row model, indented under the parent. The child
// transcript is a separate backend resource, so it loads lazily, re-polls
// with cursor deltas while the sub-agent runs, and windows deep history
// (the inspect action opens the full raw transcript).
function SubagentTrace({
  active,
  agentId,
  onInspect,
  ticket,
}: {
  active: boolean;
  agentId: string;
  onInspect?: (agentId: string) => void;
  ticket: string;
}) {
  const cacheKey = `${ticket}:${agentId}`;
  const [state, setState] = useState<ChildTraceState | null>(() => readChildTraceCache(cacheKey));
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const load = async () => {
      try {
        const cached = readChildTraceCache(cacheKey);
        // The first fetch is bounded server-side (limit) so a deep child
        // transcript never transfers whole; repeat polls are cursor deltas.
        const data = await getSubagentSession(
          ticket,
          agentId,
          cached?.cursor ?? 0,
          undefined,
          CHILD_TRACE_MAX_EVENTS,
        );
        if (cancelled) return;
        const merged = mergeChildTraceDelta(readChildTraceCache(cacheKey), data);
        writeChildTraceCache(cacheKey, merged);
        setState(merged);
      } catch {
        // Child transcript may not exist yet (or ever) — keep the parent row usable.
      }
      if (!cancelled && active) timer = window.setTimeout(load, POLL_MS);
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [active, agentId, cacheKey, ticket]);
  const events = state?.events;
  const rows = useMemo(() => {
    const traceEvents = (events ?? []).filter(
      (event) => (event.kind === "tool" && event.tool) || (event.kind === "thinking" && event.text)
    );
    return traceRows(activityTimeline(traceEvents));
  }, [events]);
  if (!rows.length) return null;
  const shown = rows.slice(-SUBTRACE_MAX_ROWS);
  const omitted = rows.length - shown.length;
  const historyNote = omitted > 0
    ? `+${omitted} earlier rows — inspect opens the full transcript`
    : state?.hasOlder
      ? "earlier rows omitted — inspect opens the full transcript"
      : null;
  return (
    <div className="session-subtrace">
      {historyNote ? (
        <div className="session-subtrace-more">{historyNote}</div>
      ) : null}
      <TraceRowList keyBase={`sub:${agentId}`} nested onInspect={onInspect} rows={shown} ticket={ticket} />
    </div>
  );
}

// OpenCode reasoning anatomy (session/index.tsx:1635-1677): one warning-hued
// line `+ Thought: <title> · <duration>`, `-` when open, body as muted
// markdown at reduced strength. Codex supplies only summaries here; Wiki
// cannot decrypt the private reasoning payload. Rows always mount collapsed
// so a transcript full of thoughts stays scannable; click to open a body.
function normalizeCodexThinkingSummary(text: string): string {
  const lines = text.split("\n");
  const first = lines[0].trim();
  const wrapped = first.match(/^\*\*([^*]+)\*\*$/);
  if (wrapped) lines[0] = wrapped[1].trim();
  return lines.join("\n");
}

export function ThinkingRow({ event, durationMs = null }: { event: SessionEvent; durationMs?: number | null }) {
  // Provider capability markers can arrive as truthy wire values after an
  // app-server round trip. Do not require a strict boolean identity.
  const codexSummary = Boolean(event.encrypted);
  const summary = codexSummary ? normalizeCodexThinkingSummary(event.text) : event.text;
  const [expanded, setExpanded] = useState(false);
  const title = summary.split("\n")[0].trim().slice(0, 120);
  const duration = durationMs !== null ? formatEventDuration(durationMs) : null;
  return (
    <div className="session-activity-row is-reasoning">
      <button
        className="session-thinking-head"
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span aria-hidden="true" className="session-thinking-prefix">{expanded ? "-" : "+"}</span>
        <span className="session-thinking-line">
          Thought
          {title || duration ? ": " : ""}
          {title ? <span className="session-thinking-title">{title}</span> : null}
          {event.partial ? <span className="session-partial-marker">partial</span> : null}
          {duration ? (
            <span className="session-thinking-duration tabular-nums">
              {title ? " · " : ""}
              {duration}
            </span>
          ) : null}
        </span>
        {event.encrypted ? <span className="session-thinking-chip">encrypted</span> : null}
      </button>
      {expanded ? (
        <div className="session-thinking">
          {/* Subtle scheme: markdown tokens damped by the container (OpenCode
              renders reasoning syntax at thinkingOpacity, theme/index.ts:292). */}
          <ShikiCode className="session-thinking-code" code={summary} lang="markdown" transparent />
        </div>
      ) : null}
    </div>
  );
}

export function ToolCallRow({
  event,
  nested = false,
  onInspect,
  ticket,
  withResult,
}: {
  event: SessionEvent;
  nested?: boolean;
  onInspect?: (agentId: string) => void;
  ticket: string;
  withResult: boolean;
}) {
  const tool = event.tool!;
  const status = toolStatus(tool);
  const running = status === "working";
  const detail = toolInlineDetail(tool);
  const { verb, target } = toolSummaryParts(tool);
  const rawOutput = tool.output ?? "";
  const parsedSegments = rawOutput ? parseHarnessOutput(rawOutput) : [];
  // A failed tool whose output carries no tagged error is still a failure:
  // plain text segments render with error semantics so the failure reads as
  // one instead of ordinary output.
  const outputSegments = tool.ok === false && !parsedSegments.some((segment) => segment.kind === "error")
    ? parsedSegments.map((segment) => segment.kind === "text" ? { ...segment, kind: "error" as const } : segment)
    : parsedSegments;
  const displayOutput = outputSegments.map((segment) => segment.text).join("\n");
  const diffSource = toolDiffSource(tool, displayOutput);
  const basePresentation = toolPresentation(tool, diffSource ?? displayOutput);
  // WIKI-253 render-layer promotion: a nominally-inline tool (read_agent,
  // list_agents, arbitrary MCP tool output, JSON dumps) with a body that's
  // long enough to be worth measuring becomes a block, so the shared collapse
  // gate in ToolOutputBody applies to every long tool result — not just the
  // ones whose sub-renderer already routes through block. Height decides the
  // final collapse; this pre-check just widens the gate's blast radius.
  const inlineWouldRenderTall = basePresentation === "inline"
    && wouldRenderTall(displayOutput, tool);
  const presentation: ToolPresentation = inlineWouldRenderTall ? "block" : basePresentation;
  const outputBlock = presentation !== "inline";
  // WIKI-253: failures default expanded so a broken run cannot hide behind a
  // toggle — the hide-error control still exists for readers who want to
  // collapse a known failure while scanning downstream rows.
  const [errorHidden, setErrorHidden] = useState(false);
  const errorShown = tool.ok === false && !errorHidden;
  const [polishedOpen, setPolishedOpen] = useState(false);
  const polishedId = useId();
  // Polished view: recognizable content only (parse or filetype hint), never
  // heuristics. Offered for successful outputs; failures keep the error flow.
  // WIKI-261: for numbered read payloads (cat -n / arrow gutter), parse the
  // gutter off, hand the stripped code to the highlighter with the path's
  // language, and route rendering through NumberedReadHighlight so real file
  // line numbers stay in a muted, non-selectable gutter column.
  const pathLang = languageForPath(toolPathHint(tool) ?? "");
  const numberedRead = pathLang ? parseNumberedPayload(displayOutput) : null;
  // For diff-presenting tools the split-diff view IS the polished form
  // (WIKI-251 HIGH#1 fix): don't offer a structured-content polished toggle
  // over "File updated." (the ack).
  const isDiffPresentation = presentation === "diff" && !!diffSource;
  const polished = tool.ok !== false && displayOutput && !isDiffPresentation
    ? detectStructuredContent(numberedRead ? numberedRead.code : displayOutput, pathLang)
    : null;
  const showOutputBlock = outputBlock && (tool.ok !== false || errorShown);
  const inlineOutput = presentation === "inline" && (tool.ok !== false || errorShown)
    ? tool.ok === false && errorShown
      ? displayOutput
      : toolInlineResult(tool, displayOutput)
    : null;
  return (
    <div
      className={`session-tool session-activity-row is-tool is-${status}${outputBlock ? " is-block" : " is-inline"}${tool.ok === false ? " is-failed" : ""}`}
      data-tool-event-id={nested ? undefined : event.id}
    >
      <div className="session-tool-head">
        {/* OpenCode's glyph micro-vocabulary in a fixed 2-char column; state
            is the row's COLOR (muted done, error failed), never a word. */}
        <span aria-hidden="true" className="session-tool-icon session-tool-icon-text">
          {toolGlyph(tool, status)}
        </span>
        <span className="session-tool-summary" title={toolSummaryLine(tool)}>
          <span className="session-tool-verb">{verb}</span>
          {target ? <><span aria-hidden="true">{" "}</span><span className="session-tool-target">{target}</span></> : null}
        </span>
        {detail ? (
          <span className="session-tool-detail" title={detail}>{detail}</span>
        ) : null}
        {running ? <span className="session-tool-running" title="running" /> : null}
        {/* Status words left the visual row (state = color, OpenCode
            index.tsx:1867-1874) but stay for screen readers. */}
        <span className="sr-only">{status}</span>
        {inlineOutput ? (
          <span className="session-tool-inline-result">
            {nested
              ? renderAnsi(clipSegmentsInline(outputSegments).map((segment) => segment.text).join("\n"))
              : isReadOrSearchTool(tool)
              ? renderAnsi(inlineOutput)
              : (
                <HarnessOutput
                  ansi
                  segments={tool.ok === false && errorShown ? outputSegments : clipSegmentsInline(outputSegments)}
                />
              )}
          </span>
        ) : null}
        {tool.ok === false && tool.output ? (
          <button
            className="session-tool-error-toggle"
            type="button"
            aria-expanded={errorShown}
            onClick={() => setErrorHidden((value) => !value)}
          >
            {errorShown ? "hide error" : "show error"}
          </button>
        ) : null}
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
        {polished ? (
          <button
            aria-controls={polishedId}
            aria-expanded={polishedOpen}
            aria-label={polishedOpen ? "hide polished output" : "show polished output"}
            aria-pressed={polishedOpen}
            className="session-tool-polished-toggle"
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setPolishedOpen((value) => !value);
            }}
          >
            polish
          </button>
        ) : null}
      </div>
      <div className="session-trace-indent">
        {polished && polishedOpen ? (
          <div className="session-tool-polished" id={polishedId}>
            <BoundedPreview
              className="session-tool-polished-preview"
              previewLines={30}
              rawText={rawOutput || polished.text}
              showSummary={false}
              text={polished.text}
              variant="block"
              renderBody={({ text }) => (
                numberedRead
                  ? <NumberedReadHighlight payload={numberedRead} lang={polished.lang} />
                  : <HighlightedCode code={text} lang={polished.lang} lineNumbers={polished.code} />
              )}
            />
          </div>
        ) : null}
        {(withResult && showOutputBlock) || (isBashTool(tool) && running) ? (
          <ToolOutputBody
            displayOutput={displayOutput}
            diffSource={diffSource}
            eventId={event.id}
            numberedRead={numberedRead}
            pathLang={pathLang}
            presentation={presentation}
            rawOutput={rawOutput}
            segments={outputSegments}
            tool={tool}
          />
        ) : null}
        {!nested && tool.agent_id ? (
          <SubagentTrace active={running} agentId={tool.agent_id} onInspect={onInspect} ticket={ticket} />
        ) : null}
      </div>
    </div>
  );
}

// Shared renderer for a flat run of trace rows — used by the activity group
// (top level) and by SubagentTrace (nested one level under an agent tool).
function TraceRowList({
  keyBase,
  nested = false,
  onInspect,
  rows,
  ticket,
}: {
  keyBase: string | number;
  nested?: boolean;
  onInspect?: (agentId: string) => void;
  rows: TraceRow[];
  ticket: string;
}) {
  return (
    <>
      {rows.map((row) => {
        const key = `${keyBase}:${row.kind}:${row.eventIndex}`;
        if (row.kind === "tool") {
          return (
            <ToolCallRow
              event={row.event}
              key={key}
              nested={nested}
              onInspect={onInspect}
              ticket={ticket}
              withResult={row.withResult}
            />
          );
        }
        return <ThinkingRow event={row.event} key={key} />;
      })}
    </>
  );
}


type ActivityTimelineItem = {
  event: SessionEvent;
  eventIndex: number;
  kind: "event" | "result";
  order: number;
  time: number | null;
};

function activityTimeline(events: SessionEvent[]): ActivityTimelineItem[] {
  const items: ActivityTimelineItem[] = [];
  events.forEach((event, eventIndex) => {
    const started = event.ts ? Date.parse(event.ts) : Number.NaN;
    items.push({
      event,
      eventIndex,
      kind: "event",
      order: eventIndex * 2,
      time: Number.isFinite(started) ? started : null,
    });
    if (event.kind !== "tool" || !event.tool) return;
    if (event.tool.output === null && event.tool.ok === null) return;
    const completed = event.tool.completed_at ? Date.parse(event.tool.completed_at) : Number.NaN;
    items.push({
      event,
      eventIndex,
      kind: "result",
      order: eventIndex * 2 + 1,
      time: Number.isFinite(completed)
        ? Math.max(completed, Number.isFinite(started) ? started : completed)
        : Number.isFinite(started) ? started : null,
    });
  });
  return items.sort((left, right) => {
    if (left.time !== null && right.time !== null && left.time !== right.time) {
      return left.time - right.time;
    }
    return left.order - right.order;
  });
}

export type SessionUiState = {
  booleans: Map<string, boolean>;
  // WIKI-253: per-tool-output "user explicitly expanded/collapsed" overrides,
  // keyed by `tool-output:<eventId>`. Lives alongside `booleans` so the store
  // is one atomic bag per session — cleared together when the session tab
  // unmounts, so numeric event ids that recycle across sessions cannot leak.
  overrides: Map<string, boolean>;
};

function createSessionUiState(): SessionUiState {
  return { booleans: new Map(), overrides: new Map() };
}

export const SessionUiStateContext = createContext<SessionUiState | null>(null);

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
  const previewLayout = useContext(GhPreviewLayoutContext);
  if (typeof href === "string" && isGitHubPreviewUrl(href)) {
    return previewLayout === "inline" ? (
      <GhPreviewInline url={href} />
    ) : (
      <GhPreviewCard url={href} />
    );
  }
  return <a href={href} {...props} {...externalLinkProps(href)} />;
}

function SessionMarkdownPre(props: ComponentProps<"pre">) {
  return <MarkdownPre detectLang {...props} />;
}

const sessionMarkdownComponents = {
  a: SessionMarkdownLink,
  pre: SessionMarkdownPre,
  table: MarkdownTable,
};

function BashBlock({ event }: { event: SessionEvent }) {
  const bash = event.bash ?? { input: "", stdout: "", stderr: "" };
  const segments: HarnessOutputSegment[] = [
    ...(bash.stdout ? [{ kind: "text" as const, text: bash.stdout }] : []),
    ...(bash.stderr ? [{ kind: "error" as const, text: bash.stderr }] : []),
  ];
  const output = segments.map((segment) => segment.text).join("\n");
  return (
    <div className="session-bash session-tool-body session-tool-block-body">
      <div className="session-tool-block-title is-command">
        $ <CommandHighlight command={bash.input || "bash"} />
      </div>
      {output ? (
        <BoundedPreview
          ansi
          className="session-tool-block-preview"
          previewLines={10}
          showSummary={false}
          text={output}
          tone={bash.stderr ? "error" : "normal"}
          variant="block"
          renderBody={({ text }) => (
            <span className="session-tool-output-text">
              {renderOutputSegments(segments, text)}
            </span>
          )}
        />
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

function claudeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function claudePath(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace(/^\/Users\/[^/]+/, "~");
}

export function ClaudeInitRow({ event, stateKey, uiState }: { event: SessionEvent; stateKey: string; uiState: SessionUiState }) {
  const [open, setOpen] = useStoredBooleanState(uiState, stateKey, false);
  const init: SessionInit = event.claude_init ?? {};
  const model = init.model || "claude";
  const cwd = claudePath(init.cwd);
  const details: Array<[string, unknown]> = [
    ["claude code version", init.claude_code_version],
    ["model", init.model],
    ["output style", init.output_style],
    ["cwd", cwd],
    ["mcp servers", init.mcp_servers],
    ["agents", init.agents],
    ["memory paths", init.memory_paths],
    ["fast mode state", init.fast_mode_state],
  ];
  return (
    <div className={`session-claude-init${open ? " is-open" : ""}`}>
      <button aria-expanded={open} className="session-claude-init-head" type="button" onClick={() => setOpen((value) => !value)}>
        <ChevronRight className={`collapse-icon${open ? "" : " is-collapsed"}`} size={12} />
        <span>session started: {model}{cwd !== "—" ? ` in ${cwd}` : ""}</span>
      </button>
      <div className={`session-collapsible session-claude-init-collapsible${open ? " is-open" : ""}`}>
        <div className="session-collapsible-inner">
          <dl className="session-claude-init-details">
            {details.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{claudeValue(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </div>
  );
}

function ClaudeTaskRow({ event }: { event: SessionEvent }) {
  const task = event.claude_task ?? {};
  const id = task.task_id || task.tool_use_id || "unknown";
  const status = task.status || "updated";
  const summary = task.summary || "task update";
  return (
    <div className={`session-claude-task is-${status}`}>
      <span className="session-claude-task-label">task {id}</span>
      <span className="session-claude-task-status">— {status}:</span>
      <span className="session-claude-task-summary">{summary}</span>
      {task.output_file ? (
        <a className="session-claude-task-output" href={task.output_file} {...externalLinkProps(task.output_file)}>
          output file
        </a>
      ) : null}
    </div>
  );
}

export function ClaudeApiRetryRow({ event }: { event: SessionEvent }) {
  const retry = event.claude_api_retry ?? {};
  const attempt = retry.attempt ?? "?";
  const max = retry.max_retries ?? "?";
  const status = retry.error_status || "error";
  const delay = typeof retry.retry_delay_ms === "number" ? retry.retry_delay_ms : 0;
  return (
    <div className="session-claude-retry" data-testid="claude-api-retry-chip">
      <AlertTriangle size={12} />
      <span className="tabular-nums">retry {attempt}/{max}</span>
      <span>— {status} in {delay}ms</span>
    </div>
  );
}

function formatRateLimitReset(resetsAt: number | null | undefined): string | null {
  if (typeof resetsAt !== "number") return null;
  const hours = Math.max(0, (resetsAt * 1000 - Date.now()) / 3_600_000);
  return `resets in ${hours < 1 ? `${Math.max(1, Math.round(hours * 60))}m` : `${Math.max(1, Math.round(hours))}h`}`;
}

export function ClaudeRateLimitChrome({ rate }: { rate: SessionRateLimit }) {
  if (!rate.status || rate.status === "allowed") return null;
  return (
    <span className="session-state-rate-limit" data-testid="claude-rate-limit-session-pill">
      <AlertTriangle size={12} />
      <span>{rate.status}</span>
      {rate.rateLimitType ? <span>{rate.rateLimitType}</span> : null}
      {typeof rate.isUsingOverage === "boolean" ? (
        <span>overage {rate.isUsingOverage ? "on" : "off"}</span>
      ) : null}
      {rate.overageStatus ? <span>overage status {rate.overageStatus}</span> : null}
      {rate.overageDisabledReason ? <span>overage reason {rate.overageDisabledReason}</span> : null}
      {formatRateLimitReset(rate.resetsAt) ? <span className="tabular-nums">{formatRateLimitReset(rate.resetsAt)}</span> : null}
    </span>
  );
}

function ClaudeRateLimitRow({ event }: { event: SessionEvent }) {
  const rate = event.claude_rate_limit ?? {};
  return (
    <div className="session-claude-rate-limit is-prominent" data-testid="claude-rate-limit-row">
      <AlertTriangle size={13} />
      <span>{rate.status || "rate limit"}</span>
      {rate.rateLimitType ? <span>{rate.rateLimitType}</span> : null}
      {typeof rate.isUsingOverage === "boolean" ? <span>overage {rate.isUsingOverage ? "on" : "off"}</span> : null}
      {rate.overageStatus ? <span>overage status {rate.overageStatus}</span> : null}
      {rate.overageDisabledReason ? <span>overage reason {rate.overageDisabledReason}</span> : null}
      {formatRateLimitReset(rate.resetsAt) ? <span className="tabular-nums">{formatRateLimitReset(rate.resetsAt)}</span> : null}
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

const MARKER_ICON: Record<MarkerSeverity, LucideIcon> = {
  info: Radio,
  warn: AlertTriangle,
  error: AlertTriangle,
};

function SyntheticSourceRow({ source, text }: { source: string; text: string }) {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return (
    <div
      className="session-synthetic-source"
      data-source={source}
      data-testid="session-synthetic-source"
    >
      <span className="session-synthetic-source-chip">[{source}]</span>
      <span className="session-synthetic-source-text">{collapsed}</span>
    </div>
  );
}

function MarkerRow({ text, marker }: { text: string; marker?: string }) {
  const rule = markerRule(marker);
  if (!rule) {
    // Non-whitelisted markers still render as a visible info row (Henry
    // prefers verbose "tool reference"-style detail over hidden chips).
    const FallbackIcon = marker === "tool_reference" ? Wrench : Radio;
    return (
      <div className="session-marker is-info" data-marker={marker}>
        <FallbackIcon size={12} />
        <span>{text}</span>
      </div>
    );
  }
  const Icon = MARKER_ICON[rule.severity];
  const rendered = rule.verb ? `${rule.verb} · ${text}` : text;
  return (
    <div className={`session-marker is-${rule.severity}`} data-marker={marker}>
      <Icon size={12} />
      <span>{rendered}</span>
    </div>
  );
}

function QuestionRow({ event }: { event: SessionEvent }) {
  const questionUi = useContext(QuestionUiContext);
  const question = event.question;
  if (!question) return null;
  const draft = questionUi?.drafts[question.tool_use_id];
  const answerDraft = draft?.answers[question.prompt];
  const resolved = questionIsResolved(question);
  const pickedIndexes = resolved
    ? question.multi_select
      ? question.answered_options ?? []
      : question.answered_option !== null
        ? [question.answered_option]
        : question.answered_options ?? []
    : answerDraft?.optionIndexes ?? [];
  const customReply = resolved
    ? question.custom_reply ?? ""
    : answerDraft?.customReply ?? "";
  const otherSelected = resolved
    ? Boolean(question.custom_reply)
    : Boolean(answerDraft?.otherSelected);
  const disabled =
    resolved || Boolean(draft?.sending) || Boolean(draft?.submitted);
  const group = questionUi?.groups.get(question.tool_use_id) ?? [event];
  const isLastQuestion = group.at(-1)?.question?.prompt === question.prompt;
  const hasMultiSelect = group.some((candidate) => candidate.question?.multi_select);
  const hasCustomSelection = group.some((candidate) => {
    const candidateQuestion = candidate.question;
    return Boolean(
      candidateQuestion &&
      (candidateQuestion.custom_reply || draft?.answers[candidateQuestion.prompt]?.otherSelected)
    );
  });
  const groupResolved = group.every(
    (candidate) => candidate.question && questionIsResolved(candidate.question),
  );
  const payload = questionGroupPayload(group, draft?.answers ?? {});
  const showSubmit = isLastQuestion && !groupResolved && (hasMultiSelect || hasCustomSelection);
  const displayedAnswers = [
    ...pickedIndexes
      .map((index) => question.options[index])
      .filter((value): value is string => Boolean(value)),
  ];
  return (
    <div className="session-question">
      <div className="session-question-head">
        <MessageCircleQuestion size={13} />
        <span>{question.header || "Question"}</span>
      </div>
      <div className="session-question-prompt">{question.prompt}</div>
      <div className="session-question-options">
        {question.options.map((option, index) => (
          <label
            className={[
              "session-question-option",
              pickedIndexes.includes(index) ? "is-picked" : "",
              !disabled ? "is-clickable" : "",
              disabled ? "is-disabled" : "",
            ].filter(Boolean).join(" ")}
            key={`${question.prompt}:${option}:${index}`}
          >
            <input
              checked={pickedIndexes.includes(index)}
              disabled={disabled}
              name={`question:${question.tool_use_id}:${question.prompt}`}
              type={question.multi_select ? "checkbox" : "radio"}
              onChange={() => questionUi?.selectOption(event, index)}
            />
            <span className="session-question-index">{index + 1}</span>
            <span>{option}</span>
          </label>
        ))}
        <div
          className={[
            "session-question-option",
            "session-question-other",
            otherSelected ? "is-picked" : "",
            !disabled ? "is-clickable" : "",
            disabled ? "is-disabled" : "",
          ].filter(Boolean).join(" ")}
        >
          <input
            aria-label={`Other answer for ${question.prompt}`}
            checked={otherSelected}
            disabled={disabled}
            name={`question:${question.tool_use_id}:${question.prompt}`}
            type={question.multi_select ? "checkbox" : "radio"}
            onChange={(changeEvent) => questionUi?.selectOther(event, changeEvent.target.checked)}
          />
          <span className="session-question-index">Other</span>
          <input
            aria-label={`Custom answer for ${question.prompt}`}
            className="session-question-other-input"
            disabled={disabled}
            placeholder="Type another answer"
            type="text"
            value={customReply}
            onFocus={() => questionUi?.selectOther(event, true)}
            onChange={(changeEvent) => questionUi?.setCustomReply(event, changeEvent.target.value)}
          />
        </div>
      </div>
      {question.multi_select ? (
        <div className="session-question-requirement">Select at least one option (required).</div>
      ) : null}
      {(resolved || draft?.submitted) && displayedAnswers.length ? (
        <div className="session-question-answer">
          <span className="session-question-answer-label">Picked</span>
          <span>{displayedAnswers.join(", ")}</span>
        </div>
      ) : null}
      {(resolved || draft?.submitted) && customReply.trim() ? (
        <div className="session-question-custom">
          <span className="session-question-answer-label">Custom reply</span>
          <span>{customReply.trim()}</span>
        </div>
      ) : null}
      {showSubmit ? (
        <div className="session-question-actions">
          <button
            disabled={payload === null || Boolean(draft?.sending) || Boolean(draft?.submitted)}
            type="button"
            onClick={() => questionUi?.submitQuestions(event)}
          >
            {draft?.sending ? "Sending…" : draft?.submitted ? "Answers sent" : "Send answers"}
          </button>
        </div>
      ) : null}
      {draft?.error ? <div className="session-question-error">{draft.error}</div> : null}
    </div>
  );
}

const MessageBlock = memo(function MessageBlock({
  event,
  imageNums,
  onInspectArtifact,
  onOpenArtifact,
  rowKey,
  sessionKey,
  ticket,
  uiState,
}: {
  event: SessionEvent;
  imageNums?: number[];
  onInspectArtifact?: (event: SessionEvent) => void;
  onOpenArtifact?: (event: SessionEvent) => void;
  rowKey: number;
  sessionKey: string;
  ticket: string;
  uiState: SessionUiState;
}) {
  if (event.kind === "artifact") {
    return <ArtifactBlock event={event} onInspect={onInspectArtifact} onOpen={onOpenArtifact} sessionKey={sessionKey} ticket={ticket} />;
  }
  if (event.kind === "user") {
    if (event.source) {
      return <SyntheticSourceRow source={event.source} text={event.text} />;
    }
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
    return <BashBlock event={event} />;
  }
  if (event.kind === "tasks") {
    return <TaskListRow event={event} stateKey={`tasks:${rowKey}`} uiState={uiState} />;
  }
  if (event.kind === "claude_init") {
    return <ClaudeInitRow event={event} stateKey={`claude-init:${rowKey}`} uiState={uiState} />;
  }
  if (event.kind === "claude_task") return <ClaudeTaskRow event={event} />;
  if (event.kind === "claude_api_retry") return <ClaudeApiRetryRow event={event} />;
  if (event.kind === "claude_rate_limit") return <ClaudeRateLimitRow event={event} />;
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
      <ReactMarkdown
        components={sessionMarkdownComponents}
        rehypePlugins={[rehypeKatex, rehypeEscapeRawHtml]}
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
      >
        {prepareTranscriptMarkdown(event.text)}
      </ReactMarkdown>
      {event.partial ? <div className="session-partial-marker">partial response</div> : null}
    </div>
  );
}, (prev, next) =>
  prev.event === next.event &&
  prev.rowKey === next.rowKey &&
  prev.onInspectArtifact === next.onInspectArtifact &&
  prev.onOpenArtifact === next.onOpenArtifact &&
  prev.ticket === next.ticket &&
  prev.uiState === next.uiState &&
  sameImageNums(prev.imageNums, next.imageNums)
);

// WIKI-247: one virtual row owns one activity event. Tool output stays paired
// with its call inside ToolCallRow, but adjacent events never share a row.
export function ActivityEventRow({
  event,
  rowKey,
  onInspect,
  thoughtDurationMs = null,
  ticket,
}: {
  event: SessionEvent;
  rowKey: number;
  onInspect?: (agentId: string) => void;
  thoughtDurationMs?: number | null;
  ticket: string;
}) {
  const rows = useMemo(() => traceRows(activityTimeline([event])), [event]);
  if (event.kind === "thinking" && event.text) {
    return (
      <div className="session-activity">
        <ThinkingRow durationMs={thoughtDurationMs} event={event} />
      </div>
    );
  }
  return (
    <div className="session-activity">
      <TraceRowList keyBase={rowKey} onInspect={onInspect} rows={rows} ticket={ticket} />
    </div>
  );
}

function useMeasuredRow(row: EventRow, onHeightChange: (row: EventRow, height: number) => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // WIKI-259: border-box for the resize callback too — the old
    // contentRect/getBoundingClientRect mix meant two box models could feed
    // the same cache. No minimum clamp: rounding a short row up to a floor
    // widens its gap beyond VIRTUAL_ROW_GAP. Zero height means a hidden
    // ancestor (display:none), not real content size — keep the last honest
    // measurement instead of stomping the cache.
    const report = () => {
      const height = el.getBoundingClientRect().height;
      if (height > 0) onHeightChange(row, Math.round(height));
    };
    report();
    const observer = new ResizeObserver(report);
    observer.observe(el);
    return () => observer.disconnect();
  }, [row, onHeightChange]);
  return ref;
}

function rowTimestamp(row: EventRow): string | null {
  return row.event.ts;
}

function rowAlign(row: EventRow): "end" | "start" {
  if (row.event.kind !== "user") return "start";
  return row.event.source ? "start" : "end";
}

function computeTimestampKeys(rows: EventRow[]): Set<number> {
  const keys = new Set<number>();
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const kind = row.event.kind;
    if (kind === "user") {
      keys.add(row.key);
      continue;
    }
    if (kind !== "assistant") continue;
    let isLast = true;
    for (let j = i + 1; j < rows.length; j += 1) {
      const laterKind = rows[j].event.kind;
      if (laterKind === "assistant") {
        isLast = false;
        break;
      }
      if (laterKind === "user") break;
    }
    if (isLast) keys.add(row.key);
  }
  return keys;
}

function currentActivityRunState(session: TranscriptSession | null): ActivityRunState {
  const inspector = session?.providerInspector;
  return activityRunStateFromProvider(
    inspector?.state,
    inspector?.pending_requests.length ?? 0,
    session?.working ?? false,
  );
}

export type TurnMeta = {
  agent: string;
  model: string | null;
  durationMs: number | null;
};

// OpenCode's turn boundary (session/index.tsx:1534-1559): `▣ Build · model ·
// duration` after the final part of each completed turn — glyph in the agent
// color, name in text, the rest muted.
export function TurnMetaRow({ meta }: { meta: TurnMeta }) {
  return (
    <div className="session-turn-meta" data-testid="session-turn-meta">
      <span aria-hidden="true" className="session-turn-meta-glyph">▣</span>
      <span className="session-turn-meta-agent">{meta.agent}</span>
      {meta.model ? <span className="session-turn-meta-detail"> · {meta.model}</span> : null}
      {meta.durationMs !== null ? (
        <span className="session-turn-meta-detail tabular-nums"> · {formatEventDuration(meta.durationMs)}</span>
      ) : null}
    </div>
  );
}

function CurrentTurnState({ runState }: { runState: Exclude<ActivityRunState, "idle"> }) {
  const state = activityStateLabel([], runState);
  const stateClass = state.replace(/\s+/g, "-");
  return (
    <div className="session-turn-live-state" data-testid="session-turn-live-state">
      <span className={`session-activity-state is-${stateClass}`}>{state}</span>
      <span className="session-activity-meta">current turn</span>
    </div>
  );
}

const VirtualSessionRow = memo(function VirtualSessionRow({
  activityRunState,
  row,
  imageNums,
  onHeightChange,
  onInspect,
  onInspectArtifact,
  onOpenArtifact,
  sessionKey,
  showTimestamp,
  thoughtDurationMs = null,
  top,
  ticket,
  turnMeta = null,
  uiState,
}: {
  activityRunState: ActivityRunState;
  row: EventRow;
  imageNums?: number[];
  onHeightChange: (row: EventRow, height: number) => void;
  onInspect?: (agentId: string) => void;
  onInspectArtifact?: (event: SessionEvent) => void;
  onOpenArtifact?: (event: SessionEvent) => void;
  sessionKey: string;
  showTimestamp: boolean;
  thoughtDurationMs?: number | null;
  top: number;
  ticket: string;
  turnMeta?: TurnMeta | null;
  uiState: SessionUiState;
}) {
  const rowRef = useMeasuredRow(row, onHeightChange);
  const style: CSSProperties = { transform: `translateY(${top}px)` };
  const ts = showTimestamp ? rowTimestamp(row) : null;
  const isActivity = row.event.kind === "tool" || row.event.kind === "thinking";
  return (
    <div
      className="session-virtual-row"
      data-align={rowAlign(row)}
      data-row-key={row.key}
      data-row-kind={isActivity ? "activity" : "message"}
      data-row-top={top}
      ref={rowRef}
      style={style}
    >
      {isActivity ? (
        <ActivityEventRow
          event={row.event}
          rowKey={row.key}
          onInspect={onInspect}
          thoughtDurationMs={thoughtDurationMs}
          ticket={ticket}
        />
      ) : (
        <>
          <MessageBlock
            event={row.event}
            imageNums={imageNums}
            onInspectArtifact={onInspectArtifact}
            onOpenArtifact={onOpenArtifact}
            rowKey={row.key}
            sessionKey={sessionKey}
            ticket={ticket}
            uiState={uiState}
          />
        </>
      )}
      {turnMeta && activityRunState === "idle" ? <TurnMetaRow meta={turnMeta} /> : null}
      {activityRunState !== "idle" ? <CurrentTurnState runState={activityRunState} /> : null}
      {ts ? <Timestamp value={ts} /> : null}
    </div>
  );
}, (prev, next) => {
  if (
    prev.activityRunState !== next.activityRunState ||
    prev.top !== next.top ||
    prev.onHeightChange !== next.onHeightChange ||
    prev.onInspect !== next.onInspect ||
    prev.onInspectArtifact !== next.onInspectArtifact ||
    prev.onOpenArtifact !== next.onOpenArtifact ||
    prev.showTimestamp !== next.showTimestamp ||
    prev.thoughtDurationMs !== next.thoughtDurationMs ||
    prev.ticket !== next.ticket ||
    prev.turnMeta !== next.turnMeta ||
    prev.uiState !== next.uiState
  ) {
    return false;
  }
  return prev.row.key === next.row.key &&
    prev.row.event === next.row.event &&
    sameImageNums(prev.imageNums, next.imageNums);
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
  archivedAt,
  showComposer = true,
  onInspect,
  onArtifactsChange,
  onInspectArtifact,
  onOpenArtifact,
  stateKey,
}: {
  ticket: string;
  subagent?: string;
  archivedAt?: string;
  showComposer?: boolean;
  onInspect?: (agentId: string) => void;
  onArtifactsChange?: (events: SessionEvent[]) => void;
  onInspectArtifact?: (event: SessionEvent) => void;
  onOpenArtifact?: (event: SessionEvent) => void;
  stateKey?: string;
}) {
  const resetKey = `${ticket}:${subagent ?? ""}:${archivedAt ?? ""}`;
  const sessionStateKey = `${stateKey ?? resetKey}:${resetKey}`;
  const rowHeightsKeyRef = useRef(resetKey);
  const rowHeightsRef = useRef<Map<number, RowMeasurement>>(new Map());
  const eventRowsCacheRef = useRef<EventRowsCache | null>(null);
  const layoutCacheRef = useRef<VirtualLayoutCache | null>(null);
  const layoutDirtyFromRef = useRef(Number.POSITIVE_INFINITY);
  const layoutRef = useRef<VirtualLayout | null>(null);
  const layoutResetKeyRef = useRef(resetKey);
  const [containerNode, setContainerNode] = useState<HTMLDivElement | null>(null);
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
    eventRowsCacheRef.current = null;
    layoutCacheRef.current = null;
    layoutDirtyFromRef.current = 0;
  }

  const target = useMemo(
    () =>
      subagent
        ? { ticket, subagent }
        : archivedAt
          ? { ticket, archivedAt }
          : { ticket },
    [archivedAt, subagent, ticket]
  );
  const visible = useElementVisible(containerNode);
  const { session, pendingUserMessages, error, refreshError, loading } = useTranscriptSession(target, visible);
  const [retrying, setRetrying] = useState(false);
  const retry = useCallback(() => {
    setRetrying(true);
    retryTranscript(target).finally(() => setRetrying(false));
  }, [target]);
  const inlineArtifactKey = `${ticket}:${subagent ?? ""}:${session?.path ?? ""}`;
  const inlineArtifactKeyRef = useRef(inlineArtifactKey);
  if (inlineArtifactKeyRef.current !== inlineArtifactKey) {
    clearInlineArtifactStates(inlineArtifactKeyRef.current);
    inlineArtifactKeyRef.current = inlineArtifactKey;
  }
  useEffect(() => {
    return () => {
      clearInlineArtifactStates(inlineArtifactKeyRef.current);
    };
  }, []);
  const [questionDrafts, setQuestionDrafts] = useState<Record<string, QuestionDraft>>({});
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);

  useEffect(() => {
    onArtifactsChange?.((session?.events ?? []).filter((event) => event.kind === "artifact" && Boolean(event.artifact_id)));
  }, [onArtifactsChange, session?.events]);

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
          return Boolean(question && questionIsResolved(question));
        })) {
          changed = true;
          continue;
        }
        next[toolUseId] = draft;
      }
      return changed ? next : current;
    });
  }, [questionGroups]);

  const sendQuestionAnswers = useCallback(async (
    toolUseId: string,
    answers: Record<string, QuestionAnswerDraft>,
    payload: Record<string, string | string[]>,
  ) => {
    setQuestionDrafts((current) => ({
      ...current,
      [toolUseId]: {
        answers,
        sending: true,
        submitted: false,
        error: null,
      },
    }));
    try {
      await respondToAgentRequest(ticket, toolUseId, { answers: payload });
      setQuestionDrafts((current) => ({
        ...current,
        [toolUseId]: {
          answers,
          sending: false,
          submitted: true,
          error: null,
        },
      }));
    } catch (submitError) {
      setQuestionDrafts((current) => ({
        ...current,
        [toolUseId]: {
          answers,
          sending: false,
          submitted: false,
          error:
            submitError instanceof Error
              ? submitError.message
              : "Could not answer question.",
        },
      }));
    }
  }, [ticket]);

  const selectOption = useCallback((event: SessionEvent, optionIndex: number) => {
    const question = event.question;
    const toolUseId = question?.tool_use_id;
    if (
      !question ||
      !toolUseId ||
      questionIsResolved(question)
    ) {
      return;
    }
    const group = questionGroups.get(toolUseId) ?? [event];
    const previous = questionDrafts[toolUseId] ?? {
      answers: {},
      sending: false,
      submitted: false,
      error: null,
    };
    if (previous.sending || previous.submitted) return;
    const currentAnswer = previous.answers[question.prompt] ?? emptyQuestionAnswer();
    const optionIndexes = question.multi_select
      ? currentAnswer.optionIndexes.includes(optionIndex)
        ? currentAnswer.optionIndexes.filter((index) => index !== optionIndex)
        : [...currentAnswer.optionIndexes, optionIndex].sort((a, b) => a - b)
      : [optionIndex];
    const nextAnswer = {
      ...currentAnswer,
      optionIndexes,
      ...(question.multi_select
        ? {}
        : { otherSelected: false, customReply: "" }),
    };
    const nextAnswers = { ...previous.answers, [question.prompt]: nextAnswer };
    const payload = questionGroupPayload(group, nextAnswers);
    const shouldAutoSubmit = !group.some((candidate) => candidate.question?.multi_select);
    if (shouldAutoSubmit && payload) {
      void sendQuestionAnswers(toolUseId, nextAnswers, payload);
      return;
    }
    setQuestionDrafts((current) => ({
      ...current,
      [toolUseId]: {
        answers: nextAnswers,
        sending: false,
        submitted: false,
        error: null,
      },
    }));
  }, [questionDrafts, questionGroups, sendQuestionAnswers]);

  const selectOther = useCallback((event: SessionEvent, selected: boolean) => {
    const question = event.question;
    const toolUseId = question?.tool_use_id;
    if (!question || !toolUseId || questionIsResolved(question)) return;
    const previous = questionDrafts[toolUseId] ?? {
      answers: {},
      sending: false,
      submitted: false,
      error: null,
    };
    if (previous.sending || previous.submitted) return;
    const currentAnswer = previous.answers[question.prompt] ?? emptyQuestionAnswer();
    const nextAnswers = {
      ...previous.answers,
      [question.prompt]: {
        ...currentAnswer,
        optionIndexes: question.multi_select ? currentAnswer.optionIndexes : [],
        otherSelected: selected,
        customReply: selected ? currentAnswer.customReply : "",
      },
    };
    setQuestionDrafts((current) => ({
      ...current,
      [toolUseId]: {
        answers: nextAnswers,
        sending: false,
        submitted: false,
        error: null,
      },
    }));
  }, [questionDrafts]);

  const setCustomReply = useCallback((event: SessionEvent, value: string) => {
    const question = event.question;
    const toolUseId = question?.tool_use_id;
    if (!question || !toolUseId || questionIsResolved(question)) return;
    const previous = questionDrafts[toolUseId] ?? {
      answers: {},
      sending: false,
      submitted: false,
      error: null,
    };
    if (previous.sending || previous.submitted) return;
    const currentAnswer = previous.answers[question.prompt] ?? emptyQuestionAnswer();
    const nextAnswers = {
      ...previous.answers,
      [question.prompt]: {
        ...currentAnswer,
        optionIndexes: question.multi_select ? currentAnswer.optionIndexes : [],
        otherSelected: true,
        customReply: value,
      },
    };
    setQuestionDrafts((current) => ({
      ...current,
      [toolUseId]: {
        answers: nextAnswers,
        sending: false,
        submitted: false,
        error: null,
      },
    }));
  }, [questionDrafts]);

  const submitQuestions = useCallback((event: SessionEvent) => {
    const toolUseId = event.question?.tool_use_id;
    if (!toolUseId) return;
    const group = questionGroups.get(toolUseId) ?? [event];
    const draft = questionDrafts[toolUseId];
    if (!draft || draft.sending || draft.submitted) return;
    const payload = questionGroupPayload(group, draft.answers);
    if (!payload) return;
    void sendQuestionAnswers(toolUseId, draft.answers, payload);
  }, [questionDrafts, questionGroups, sendQuestionAnswers]);

  const questionUi = useMemo<QuestionUiContextValue>(() => ({
    drafts: questionDrafts,
    groups: questionGroups,
    selectOption,
    selectOther,
    setCustomReply,
    submitQuestions,
  }), [questionDrafts, questionGroups, selectOption, selectOther, setCustomReply, submitQuestions]);

  const displayEvents = useMemo(
    () => {
      if (!session) return [];
      return [
        ...mergeComposerEvents(
          applyComposerSources(session.events, session.composerMessages),
          composerMessageEvents(session),
        ),
        ...modelChangedMarkers(session),
      ];
    },
    [session?.events, session?.providerInspector, session?.composerMessages],
  );

  const rowResult = useMemo(() => {
    const result = eventRowsIncremental(
      displayEvents,
      session?.base ?? 0,
      eventRowsCacheRef.current,
      session?.eventsChangedFrom,
    );
    eventRowsCacheRef.current = result.cache;
    return result;
  }, [displayEvents, session?.base, session?.eventsChangedFrom]);
  const rows = rowResult.rows;
  const layout = useMemo(() => {
    const changedFrom = Math.min(rowResult.changedFrom, layoutDirtyFromRef.current);
    const result = buildVirtualLayoutIncremental(
      rows,
      rowHeightsRef.current,
      rowHeightVersion,
      layoutCacheRef.current,
      Number.isFinite(changedFrom) ? changedFrom : undefined,
    );
    layoutCacheRef.current = result.cache;
    layoutDirtyFromRef.current = Number.POSITIVE_INFINITY;
    return result.layout;
  }, [rowResult.changedFrom, rows, resetKey, rowHeightVersion]);
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

  const reportRowHeight = useCallback((row: EventRow, height: number) => {
    const measurement: RowMeasurement = {
      height,
      refs: [row.event],
    };
    const current = rowHeightsRef.current.get(row.key);
    if (current && current.height === measurement.height && sameEventRefs(current.refs, measurement.refs)) {
      return;
    }
    rowHeightsRef.current.set(row.key, measurement);
    const index = layoutCacheRef.current?.layout.keyToIndex.get(row.key);
    if (index !== undefined) {
      layoutDirtyFromRef.current = Math.min(layoutDirtyFromRef.current, index);
    }
    setRowHeightVersion((version) => version + 1);
  }, []);

  const loadOlder = useCallback(async () => {
    if (!session || loadingOlder) return;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      await loadOlderEvents(ticket, session.base, 500);
    } catch (loadError) {
      setOlderError(loadError instanceof Error ? loadError.message : "Could not load older events");
    } finally {
      setLoadingOlder(false);
    }
  }, [loadingOlder, session, ticket]);

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
  const visibleRows = useMemo(() => {
    const end = Math.min(visibleRange.end, rows.length - 1);
    if (end < visibleRange.start) return [];
    const visible: { row: EventRow; top: number }[] = [];
    for (let index = visibleRange.start; index <= end; index += 1) {
      const row = rows[index];
      if (!row) continue;
      visible.push({ row, top: layout.tops[index] ?? 0 });
    }
    return visible;
  }, [layout.tops, rows, visibleRange.end, visibleRange.start]);
  const timestampKeys = useMemo(() => computeTimestampKeys(rows), [rows]);
  const activityRunState = currentActivityRunState(session);
  const currentTurnStateKey = activityRunState === "idle" ? null : rows.at(-1)?.key ?? null;
  const sessionWorking = session?.working ?? false;
  const sessionModel = session?.model ?? null;
  const sessionAgentName = session?.sessionMeta.agent_name ?? session?.kind ?? null;
  const thoughtDurationByKey = useMemo(() => thoughtDurations(rows), [rows]);
  // Stable per-key TurnMeta objects so row memoization holds between renders.
  const turnMetaByKey = useMemo(() => {
    const metas = new Map<number, TurnMeta>();
    if (!sessionAgentName && !sessionModel) return metas;
    for (const [key, durationMs] of turnMetaDurations(rows, sessionWorking)) {
      metas.set(key, { agent: sessionAgentName ?? "agent", model: sessionModel, durationMs });
    }
    return metas;
  }, [rows, sessionAgentName, sessionModel, sessionWorking]);

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
  const composerGuidance = getComposerGuidance(session?.working ?? false);

  // K/J recall in the composer — user messages from the transcript itself
  // (covers terminal-typed AND wiki-sent, no separate storage). Synthetic
  // fleet/steer/mastermind turns are system messages, not Henry input, so
  // they must not surface as recall candidates. Apply the composer-source
  // correlation first so that composer_messages-tagged turns are excluded
  // alongside transcript-tagged ones.
  const userHistory = useMemo(() => {
    if (!session) return [];
    const enriched = applyComposerSources(session.events, session.composerMessages);
    return enriched
      .filter(
        (event) =>
          event.kind === "user" &&
          !event.source &&
          event.text.length <= 2000,
      )
      .map((event) => event.text)
      .slice(-50);
  }, [session]);

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
        <div className="session-tab" ref={setContainerNode}>
          <div className="session-empty session-empty-error" role="alert">
            <div className="session-empty-title">Could not load transcript</div>
            <div className="session-empty-body">{error}</div>
            <button
              type="button"
              className="session-empty-retry"
              onClick={retry}
              disabled={retrying}
            >
              <RefreshCw size={12} />
              {retrying ? "Retrying…" : "Try again"}
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="session-tab" ref={setContainerNode}>
        <div className="session-empty">
          <LoadingPlaceholder className="session-loading" lines={[82, 96, 74, 88]} />
        </div>
      </div>
    );
  }

  const rateLimit = session.sessionMeta.rate_limit;
  const inspector = session.providerInspector;

  return (
    <QuestionUiContext.Provider value={questionUi}>
      <SessionUiStateContext.Provider value={uiState}>
      <div className="session-tab" ref={setContainerNode}>
      {refreshError ? (
        <div className="session-stale-banner" role="status" data-testid="session-stale-banner">
          <span className="session-stale-label">stale</span>
          <span className="session-stale-body">
            Refresh failed — showing last-loaded transcript.
          </span>
          <button
            type="button"
            className="session-stale-retry"
            onClick={retry}
            disabled={retrying}
          >
            <RefreshCw size={12} />
            {retrying ? "Retrying…" : "Retry"}
          </button>
        </div>
      ) : null}
      {(session.tasks.length > 0 || session.pr || session.sessionMeta.custom_title || session.sessionMeta.agent_name || (rateLimit?.status && rateLimit.status !== "allowed")) ? (
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
          {rateLimit ? <ClaudeRateLimitChrome rate={rateLimit} /> : null}
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
      {inspector ? (
        <ProviderActionRequired inspector={inspector} ticket={ticket} />
      ) : null}
      {inspector && inspector.provider === "codex" ? (
        <CodexStreamHighlights
          events={inspector.events}
          currentTurnDiff={
            inspector.current_turn_diff === undefined
              ? undefined
              : inspector.current_turn_diff?.diff ?? null
          }
        />
      ) : null}
      <div className="session-scroll" ref={ref}>
        <div className="session-scroll-inner" ref={innerRef}>
          {session.hasOlder && !subagent ? (
            <div className="session-load-older">
              <button disabled={loadingOlder} onClick={() => void loadOlder()} type="button">
                {loadingOlder ? "Loading older events…" : "Load older events"}
              </button>
              {olderError ? <span role="alert">{olderError}</span> : null}
            </div>
          ) : null}
          {displayEvents.length === 0 && pendingUserMessages.length === 0 ? (
            <div
              className="session-zero-events"
              role="status"
              data-testid="session-zero-events"
            >
              <div className="session-zero-events-title">
                {session.working ? "Waiting for the first event…" : "No events yet"}
              </div>
              <div className="session-zero-events-body">
                {session.working
                  ? "The agent is running but has not produced output yet."
                  : subagent
                    ? "This subagent hasn't emitted any events."
                    : showComposer
                      ? "Send a message below to start the session."
                      : "This session has no events."}
              </div>
            </div>
          ) : null}
          <div className="session-virtual-list" style={{ height: layout.totalHeight }}>
            {visibleRows.map(({ row, top }) => (
              <VirtualSessionRow
                activityRunState={
                  row.key === currentTurnStateKey
                    ? activityRunState
                    : "idle"
                }
                row={row}
                imageNums={row.event.kind === "user" ? imageNumbers.get(row.event) : undefined}
                key={row.key}
                onHeightChange={reportRowHeight}
                onInspect={onInspect}
                onInspectArtifact={onInspectArtifact}
                onOpenArtifact={onOpenArtifact}
                sessionKey={inlineArtifactKey}
                showTimestamp={timestampKeys.has(row.key)}
                thoughtDurationMs={thoughtDurationByKey.get(row.key) ?? null}
                ticket={ticket}
                top={top}
                turnMeta={turnMetaByKey.get(row.key) ?? null}
                uiState={uiState}
              />
            ))}
          </div>
        </div>
      </div>
      {subagent || !showComposer ? null : (
        <MessageComposer
          history={userHistory}
          pending={pendingUserMessages}
          queued={session.queue}
          runningSubagents={runningSubagents}
          stateKey={composerStateKeyForSession(ticket, subagent)}
          thinking={session.working}
          ticket={ticket}
          guidance={composerGuidance}
          onInspect={onInspect}
        />
      )}
      <div className="session-footer tabular-nums">
        <SessionModelFooter session={session} ticket={ticket} />
        {subagent || !showComposer ? null : (
          <div aria-hidden="true" className="session-footer-hints">
            {composerGuidance.shortcuts.map((shortcut) => (
              <span className="session-hint" key={shortcut.key}>
                <span className="session-hint-key">{shortcut.key}</span>{" "}
                {shortcut.action}
              </span>
            ))}
          </div>
        )}
      </div>
      </div>
      </SessionUiStateContext.Provider>
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
  pending = [],
  queued = [],
  runningSubagents = [],
  thinking = false,
  guidance,
  onInspect,
}: {
  stateKey: string;
  ticket: string;
  history?: string[];
  pending?: PendingUserMessage[];
  queued?: QueuedMessage[];
  runningSubagents?: SubagentInfo[];
  thinking?: boolean;
  guidance: ComposerGuidance;
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
  const [composerFocused, setComposerFocused] = useState(false);
  const pendingKeyRef = useRef<string | null>(null);
  const registerRef = useRef<string>("");
  const historyPosRef = useRef<number | null>(null);
  const draftRef = useRef("");
  const [attachments, setAttachments] = useState<{ path: string; url: string }[]>([]);
  // (inputRef & visual refs declared below with the menu state)
  const skills = useSkills();
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [activeCommand, setActiveCommand] = useState<ComposerCommand | null>(null);
  const [commandValues, setCommandValues] = useState<Record<string, string>>({});
  const [commandBusy, setCommandBusy] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  // Round-7 REVIEW [MEDIUM]: multi-line composer drafts must survive
  // opening a structured command. We capture the composer slices around
  // the `/foo` sigil at open time and stitch them back on cancel or
  // successful run so `existing draft\n/sp` no longer discards
  // `existing draft`.
  const [commandDraftContext, setCommandDraftContext] = useState<
    { before: string; after: string } | null
  >(null);
  // Round-7 REVIEW [MEDIUM]: a passing `/gate` or `/archive` used to
  // clear the form silently, contradicting the success-summary
  // contract. Persist a short-lived notice so the result is visible.
  const [commandNotice, setCommandNotice] = useState<
    { summary: string; detail?: string } | null
  >(null);
  // Round-7 REVIEW [HIGH]: synchronous guard for double-submit. React
  // schedules `commandBusy = true`, but two synchronous submit events in
  // the same tick see the old value; this ref flips immediately.
  const commandInFlightRef = useRef(false);
  const visualAnchorRef = useRef(0);
  const visualHeadRef = useRef(0);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const measureRef = useRef<HTMLTextAreaElement | null>(null);
  const composerInputId = useId();
  const composerHelpId = `${composerInputId}-help`;
  const [caretPos, setCaretPos] = useState(selectionRef.current.start);
  const [narrowComposer, setNarrowComposer] = useState(false);
  const [overlayPos, setOverlayPos] = useState<{ top: number; left: number; char: string } | null>(null);

  useLayoutEffect(() => {
    const node = composerRef.current;
    if (!node) return;
    const update = () => setNarrowComposer(node.clientWidth <= COMPOSER_NARROW_WIDTH);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

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

  // Overlay invalidation beyond text/caret changes: textarea scroll moves the
  // caret's viewport position, and layout reflow (pane resize) changes line
  // wrapping. Both bump a tick that re-runs the measurement effect.
  const [overlayTick, setOverlayTick] = useState(0);
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    const bump = () => setOverlayTick((tick) => tick + 1);
    // Native listener (not React-synthetic): programmatic scrollTop writes
    // and browser-driven caret scrolling both fire here reliably. Re-bind on
    // focus transitions so the listener always tracks the live node, and
    // re-measure once on every (re)bind.
    el.addEventListener("scroll", bump, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(bump);
    observer?.observe(el);
    bump();
    return () => {
      el.removeEventListener("scroll", bump);
      observer?.disconnect();
    };
  }, [composerFocused]);

  // WIKI-244: the cursor is a block in every vim mode. Insert mode hides the
  // native line caret (CSS) and draws the same overlay block that normal mode
  // uses at end-of-line, so the cursor never changes shape across mode
  // switches. Normal/visual mode keeps the one-char-selection block for
  // positions that have a character under them.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el || document.activeElement !== el) {
      setOverlayPos(null);
      return;
    }
    // Insert mode reads the live DOM selection: programmatic value/selection
    // changes (paste, external fill) update the DOM without firing the
    // select/keyup events that keep caretPos state in sync, and a stale
    // caretPos would paint the block over the wrong character or hide it.
    const domCaret = el.selectionStart ?? caretPos;
    const at = Math.min(vimMode === "insert" ? domCaret : caretPos, text.length);
    if (vimMode !== "insert") {
      const needsOverlay = text.length === 0 || at >= text.length || text[at] === "\n";
      if (!needsOverlay) {
        setOverlayPos(null);
        return;
      }
    }
    const pos = measureCaret(el, at);
    // When the caret line is scrolled out of the textarea's visible box, hide
    // the block — a cursor floating over unrelated rows is worse than none.
    if (pos.top < -2 || pos.top > el.clientHeight - 4) {
      setOverlayPos(null);
      return;
    }
    // The covered glyph is captured with the measurement, from the same live
    // index — rendering from separate state could show a stale character
    // after programmatic selection changes (WIKI-244 R6 M4).
    const under = el.value[at];
    setOverlayPos({ ...pos, char: under && under !== "\n" ? under : "" });
  }, [text, caretPos, vimMode, composerFocused, overlayTick]);

  const trigger = (() => {
    const el = inputRef.current;
    const caretAt = el && document.activeElement === el ? el.selectionStart : text.length;
    const slice = text.slice(0, caretAt ?? text.length);
    const match = SKILL_TRIGGER.exec(slice);
    if (!match) return null;
    const start = (caretAt ?? 0) - match[2].length - 1;
    // \/foo escape — backslash immediately before sigil suppresses trigger
    if (start > 0 && text[start - 1] === "\\") return null;
    // command menu only fires on `/` (not `$`) at start-of-line — first char or preceded by newline
    const atLineStart =
      match[1] === "/" && (start === 0 || text[start - 1] === "\n");
    return {
      sigil: match[1],
      partial: match[2],
      start,
      atLineStart,
    };
  })();
  const commandMatches =
    trigger?.atLineStart && vimMode === "insert" && !menuDismissed
      ? filterCommands(trigger.partial).slice(0, 8)
      : [];
  const menuItems =
    trigger && commandMatches.length === 0 && vimMode === "insert" && !menuDismissed
      ? skills.filter((s) => s.name.startsWith(trigger.partial)).slice(0, 8)
      : [];

  const menuOpen = commandMatches.length > 0 || menuItems.length > 0;
  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node | null;
      if (!target) return;
      if (inputRef.current && inputRef.current.contains(target)) return;
      const menuEl = document.getElementById(SLASH_MENU_ID);
      if (menuEl && menuEl.contains(target)) return;
      const skillMenu = document.querySelector(".session-skill-menu");
      if (skillMenu && skillMenu.contains(target)) return;
      setMenuDismissed(true);
    }
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [menuOpen]);

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

  function acceptCommand(command: ComposerCommand) {
    // Snapshot the composer text around the `/foo` sigil so cancel /
    // success can restore any surrounding multi-line draft. Round-7
    // REVIEW [MEDIUM] (session.tsx:2645).
    const before = trigger ? text.slice(0, trigger.start) : "";
    const after = trigger
      ? text.slice(trigger.start + 1 + trigger.partial.length)
      : "";
    setCommandDraftContext({ before, after });
    setActiveCommand(command);
    setCommandValues(initialValues(command));
    setCommandBusy(false);
    setCommandError(null);
    setCommandNotice(null);
    commandInFlightRef.current = false;
    setMenuIndex(0);
    setMenuDismissed(false);
    setText("");
    rememberSelection(0);
  }

  function cancelCommand({ restoreText = true }: { restoreText?: boolean } = {}) {
    // Belt-and-braces: `<CommandForm>` blocks cancel while busy, but the
    // programmatic path (e.g. keyboard shortcuts wired elsewhere) is
    // guarded here too so an in-flight destructive command can't be
    // pulled out from under the user. Round-7 REVIEW [MEDIUM].
    if (commandBusy) return;
    const command = activeCommand;
    const values = commandValues;
    const draft = commandDraftContext;
    setActiveCommand(null);
    setCommandValues({});
    setCommandBusy(false);
    setCommandError(null);
    setCommandDraftContext(null);
    commandInFlightRef.current = false;
    if (restoreText && command) {
      const fallback = serializeCommand(command, values);
      const before = draft?.before ?? "";
      const after = draft?.after ?? "";
      const restored = `${before}${fallback}${after}`;
      setText(restored);
      requestAnimationFrame(() => {
        const pos = before.length + fallback.length;
        inputRef.current?.setSelectionRange(pos, pos);
        inputRef.current?.focus();
        rememberSelection(pos);
      });
    } else {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }

  async function runCommand() {
    if (!activeCommand || commandBusy) return;
    // Round-7 REVIEW [HIGH]: block double-fire *within the same tick*.
    // Two synchronous submit events (e.g. Meta+Enter routing through
    // both a form-level and keydown-level handler) would otherwise both
    // pass the React-state check and dispatch.
    if (commandInFlightRef.current) return;
    const missing = missingRequired(activeCommand, commandValues);
    if (missing.length > 0) {
      setCommandError(`Fill required arg: ${missing.map((arg) => arg.name).join(", ")}`);
      return;
    }
    commandInFlightRef.current = true;
    setCommandBusy(true);
    setCommandError(null);
    const command = activeCommand;
    const draft = commandDraftContext;
    try {
      const result = await command.dispatch(commandValues, { ticket });
      if (!result.ok) {
        setCommandError(result.detail ? `${result.summary} · ${result.detail}` : result.summary);
        return;
      }
      const notice: { summary: string; detail?: string } = { summary: result.summary };
      if (result.detail) notice.detail = result.detail;
      setCommandNotice(notice);
      setActiveCommand(null);
      setCommandValues({});
      setCommandDraftContext(null);
      // Restore surrounding draft (the parts of the composer that
      // weren't part of the `/foo` invocation). Round-7 REVIEW [MEDIUM].
      const before = draft?.before ?? "";
      const after = draft?.after ?? "";
      const restored = `${before}${after}`;
      setText(restored);
      if (restored.length > 0) {
        requestAnimationFrame(() => {
          const pos = before.length;
          inputRef.current?.setSelectionRange(pos, pos);
          inputRef.current?.focus();
          rememberSelection(pos);
        });
      }
    } catch (err) {
      setCommandError(err instanceof Error ? err.message : "Command failed");
    } finally {
      setCommandBusy(false);
      commandInFlightRef.current = false;
    }
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

  async function deliverPending(
    message: Pick<PendingUserMessage, "id" | "text" | "mode">,
  ) {
    setBusy(true);
    setError(null);
    try {
      const result = await sendAgentMessage(ticket, message.text, message.mode, message.id);
      historyPosRef.current = null;
      if (result.messages) {
        replaceTranscriptQueue(ticket, result.messages, {
          pendingId: message.id,
          source: message.mode === "now" ? "auto" : "explicit",
          text: message.text,
          position: result.position,
        });
      }
      if (result.status !== "queued") {
        updatePendingUserMessage(ticket, message.id, { status: "sent", error: undefined });
        refreshTranscript(ticket);
      }
    } catch (err) {
      updatePendingUserMessage(ticket, message.id, {
        status: "failed",
        error: err instanceof Error ? err.message : "Send failed",
      });
    } finally {
      setBusy(false);
    }
  }

  async function send(mode: "now" | "on-idle") {
    const raw = text.trim();
    if (!raw || busy) return;
    // \/foo escape — user typed \/steer to send literal /steer
    const value = raw.startsWith("\\/") ? raw.slice(1) : raw;
    setBusy(true);
    setError(null);
    setText("");
    setAttachments([]);
    rememberSelection(0);
    const message = {
      id: crypto.randomUUID(),
      text: value,
      mode,
    };
    addPendingUserMessage(ticket, message);
    await deliverPending(message);
  }

  async function retry(message: PendingUserMessage) {
    if (busy) return;
    retryPendingUserMessage(ticket, message.id);
    await deliverPending(message);
  }

  function edit(message: PendingUserMessage) {
    if (busy) return;
    removePendingUserMessage(ticket, message.id);
    setText(message.text);
    setVimMode("insert");
    setInsertCaret(message.text.length);
    inputRef.current?.focus();
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
    <div
      className="session-composer"
      data-compact={narrowComposer ? "true" : undefined}
      data-history-count={history.length}
      ref={composerRef}
    >
      {pending.map((message) => (
        <div
          className={`session-user session-pending-user is-${message.status}`}
          data-status={message.status}
          key={message.id}
        >
          <UserText text={message.text} />
          <div className="session-pending-status">
            {message.status === "sending" ? (
              <>
                <CircleDashed className="session-pending-spinner" size={11} />
                <span>sending…</span>
              </>
            ) : message.status === "sent" ? (
              <>
                <CircleCheck size={11} />
                <span>sent · waiting for transcript</span>
              </>
            ) : (
              <>
                <AlertTriangle size={11} />
                <span title={message.error}>send failed</span>
                <button type="button" onClick={() => void retry(message)}>Retry send</button>
                <button type="button" onClick={() => edit(message)}>Edit message</button>
              </>
            )}
          </div>
        </div>
      ))}
      {queued.map((message, i) => (
        <div
          className={`session-queued is-${message.source ?? "explicit"}`}
          key={message.pending_id ?? `${message.queued_at}-${i}`}
          tabIndex={0}
          title={message.source === "auto"
            ? "Waiting for current turn to finish"
            : "Queued for next idle boundary"}
        >
          <Hourglass size={11} />
          <span className="session-queued-text">{message.text}</span>
          <button aria-label="Cancel queued message" type="button" onClick={() => cancel(i)}>
            <X size={12} />
          </button>
          <span className="session-queued-tooltip" role="tooltip">
            {message.source === "auto"
              ? "Waiting for current turn to finish"
              : "Queued for next idle boundary"}
          </span>
        </div>
      ))}
      {commandMatches.length > 0 ? (
        <SlashMenu
          commands={commandMatches}
          activeIndex={Math.min(menuIndex, commandMatches.length - 1)}
          onSelect={acceptCommand}
          onHover={setMenuIndex}
        />
      ) : null}
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
      {commandNotice ? (
        <div
          className="composer-command-notice"
          role="status"
          aria-live="polite"
        >
          <span className="composer-command-notice-summary">
            {commandNotice.summary}
          </span>
          {commandNotice.detail ? (
            <span className="composer-command-notice-detail">
              {commandNotice.detail}
            </span>
          ) : null}
          <button
            className="composer-command-notice-dismiss"
            type="button"
            aria-label="Dismiss command result"
            onClick={() => setCommandNotice(null)}
          >
            ×
          </button>
        </div>
      ) : null}
      {activeCommand ? (
        <CommandForm
          command={activeCommand}
          values={commandValues}
          onChange={(name, value) =>
            setCommandValues((current) => ({ ...current, [name]: value }))
          }
          onSubmit={() => void runCommand()}
          onCancel={() => cancelCommand()}
          busy={commandBusy}
          error={commandError}
        />
      ) : (
        <>
          <div className="session-composer-row">
            <label className="session-composer-target" htmlFor={composerInputId}>
              <span>ask or steer</span>
              <strong>{ticket}</strong>
            </label>
            <div className="session-input-wrap">
              {overlayPos ? (
                <span
                  aria-hidden="true"
                  className="session-empty-block-cursor"
                  style={{ top: overlayPos.top, left: overlayPos.left }}
                >
                  {overlayPos.char}
                </span>
              ) : null}
              <textarea
                id={composerInputId}
                autoCapitalize="off"
                autoCorrect="off"
                className={vimMode === "normal" || vimMode === "visual" ? "is-vim-normal" : undefined}
                spellCheck={false}
                placeholder={
                  vimMode === "insert"
                    ? narrowComposer
                      ? thinking
                        ? `Steer ${ticket}…`
                        : `Ask ${ticket}…`
                      : `Ask a question or give ${ticket} a new direction…`
                    : undefined
                }
                aria-describedby={composerHelpId}
                ref={inputRef}
                rows={2}
                value={text}
                role="combobox"
                aria-expanded={commandMatches.length > 0}
                aria-controls={commandMatches.length > 0 ? SLASH_MENU_ID : undefined}
                aria-activedescendant={
                  commandMatches.length > 0
                    ? slashMenuOptionId(Math.min(menuIndex, commandMatches.length - 1))
                    : undefined
                }
                aria-autocomplete="list"
                onFocus={(event) => {
                  setComposerFocused(true);
                  if (vimMode !== "insert") enterInsert(event.currentTarget.selectionEnd ?? text.length);
                  else captureSelection(event.currentTarget);
                }}
                onBlur={() => setComposerFocused(false)}
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
                  // IME safety: during a composition session key events carry
                  // intermediate composition state, not vim commands or menu
                  // navigation — let the browser handle them untouched.
                  if (event.nativeEvent.isComposing) return;
                  if (vimMode === "normal") {
                    handleNormalKey(event);
                    return;
                  }
                  if (vimMode === "visual") {
                    handleVisualKey(event);
                    return;
                  }
                  if (commandMatches.length > 0) {
                    if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "j")) {
                      event.preventDefault();
                      setMenuIndex((i) => (i + 1) % commandMatches.length);
                      return;
                    }
                    if (
                      event.key === "ArrowUp" ||
                      (event.ctrlKey && event.key === "k") ||
                      (event.key === "Tab" && event.shiftKey)
                    ) {
                      event.preventDefault();
                      setMenuIndex((i) => (i - 1 + commandMatches.length) % commandMatches.length);
                      return;
                    }
                    if (event.key === "Enter" || (event.key === "Tab" && !event.shiftKey)) {
                      event.preventDefault();
                      const chosen = commandMatches[Math.min(menuIndex, commandMatches.length - 1)];
                      acceptCommand(chosen);
                      return;
                    }
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setMenuDismissed(true);
                      return;
                    }
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
              aria-label={guidance.sendAction}
              title={guidance.sendAction}
              type="button"
              onClick={() => void send("now")}
            >
              <span className="session-send-label">send</span>
              <SendHorizontal aria-hidden="true" size={14} />
            </button>
          </div>
        </>
      )}
      <div id={composerHelpId} className="session-composer-help">
        {guidance.accessibleText}
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
        {vimMode !== "insert" ? (
          <span className={`session-vim-mode is-${vimMode}`}>
            {vimMode === "visual"
              ? "-- VISUAL --"
              : vimMode === "pane"
                ? "-- PANE --"
                : "-- NORMAL --"}
          </span>
        ) : null}
      </div>
    </div>
  );
}

// SessionTab plus a self-contained fullscreen-inspector scope: artifacts in
// this transcript get the Fullscreen action and Cmd+Enter targeting, with
// sibling navigation limited to this transcript.
export function InspectableSessionTab(props: ComponentProps<typeof SessionTab>) {
  const scopeRef = useRef<HTMLDivElement | null>(null);
  const { handleArtifactsChange, inspector, openInspector } = useArtifactInspector({
    scopeRef,
    ticket: props.ticket,
  });
  return (
    <div className="session-artifact-scope" ref={scopeRef}>
      <SessionTab
        {...props}
        onArtifactsChange={handleArtifactsChange}
        onInspectArtifact={openInspector}
      />
      {inspector}
    </div>
  );
}

const WIDTH_KEY = "wiki-session-sidebar-width";
const WIDTH_CUSTOM_KEY = "wiki-session-sidebar-width-custom";
const MIN_WIDTH = 320;
// WIKI-251 HIGH#4: while the reader hasn't customized the width the pane
// width is a pure CSS expression — max(47vw, 560px floor), capped at 70vw,
// never below 320px. The browser recomputes on resize with no JS listener,
// so the default tracks the viewport at every screen size (not just at
// mount). Once the user drags we persist a px value and lock to it.
const DEFAULT_WIDTH_RATIO = 0.47;
const DEFAULT_WIDTH_FLOOR = 560;
const MAX_WIDTH_RATIO = 0.7;

// The CSS-side default. Keep the numerics in sync with the constants above.
// Used by the JSX below and asserted by the wiki-251 playwright fixture at
// 1280/1440/1920/2560 without a JS resize listener.
export const DEFAULT_WIDTH_CSS = `max(${MIN_WIDTH}px, min(${MAX_WIDTH_RATIO * 100}vw, max(${DEFAULT_WIDTH_RATIO * 100}vw, ${DEFAULT_WIDTH_FLOOR}px)))`;

export function computeDefaultWidth(viewport: number): number {
  const preferred = Math.round(viewport * DEFAULT_WIDTH_RATIO);
  const withFloor = Math.max(preferred, DEFAULT_WIDTH_FLOOR);
  return Math.min(Math.max(withFloor, MIN_WIDTH), Math.round(viewport * MAX_WIDTH_RATIO));
}

export function clampSidebarWidth(width: number, viewport: number): number {
  return Math.min(Math.max(width, MIN_WIDTH), Math.round(viewport * MAX_WIDTH_RATIO));
}

function clampWidth(width: number, viewport: number): number {
  return clampSidebarWidth(width, viewport);
}

export type SidebarTarget = {
  ticket: string;
  kind: string | null;
  role: string | null;
  model?: string | null;
  pr?: string | null;
  canReview?: boolean;
  // WIKI-229: reserved for per-archive selection when a ticket has
  // multiple archives. Threaded end-to-end (SidebarTarget → SessionTab →
  // TranscriptTarget → getAgentSession → /session?archived_at=…) but not
  // set in this PR — the backend route currently returns the newest
  // archive regardless, and WIKI-229 delivers the discriminated route.
  archivedAt?: string;
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
  // WIKI-251 HIGH#4: isCustom tracks whether the user has ever dragged the
  // resize handle. Until they do, the pane tracks viewport at ~47% and a
  // window resize picks up the new ratio. After a drag we lock to a px value
  // and re-mounts / resizes keep the persisted pixel width.
  const [isCustom, setIsCustom] = useState<boolean>(() => {
    if (typeof localStorage === "undefined") return false;
    return localStorage.getItem(WIDTH_CUSTOM_KEY) === "1"
      && Number(localStorage.getItem(WIDTH_KEY)) > 0;
  });
  const [customWidth, setCustomWidth] = useState<number>(() => {
    if (typeof localStorage === "undefined") return 0;
    const stored = Number(localStorage.getItem(WIDTH_KEY));
    return stored > 0 ? stored : 0;
  });
  // Resize listener only re-clamps the CUSTOM px width against the current
  // viewport (so the persisted px stays within the 70vw cap when the window
  // shrinks). The default path is pure CSS — no state, no listener.
  const [viewportWidth, setViewportWidth] = useState<number>(
    () => (typeof window !== "undefined" ? window.innerWidth : 1440),
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!isCustom) return;
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [isCustom]);

  const width: string | number = isCustom
    ? clampWidth(customWidth || computeDefaultWidth(viewportWidth), viewportWidth)
    : DEFAULT_WIDTH_CSS;

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* synthetic events lack a real pointer — move/up listeners still work */
    }
    const onMove = (move: PointerEvent) => {
      const next = clampWidth(window.innerWidth - move.clientX, window.innerWidth);
      setCustomWidth(next);
      setIsCustom(true);
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      setCustomWidth((current) => {
        if (current > 0) {
          localStorage.setItem(WIDTH_KEY, String(current));
          localStorage.setItem(WIDTH_CUSTOM_KEY, "1");
        }
        return current;
      });
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  };

  return (
    <aside
      className={`session-sidebar${isCustom ? "" : " is-default-width"}`}
      data-width-mode={isCustom ? "custom" : "responsive"}
      style={{ width }}
    >
      <div className="session-resize" onPointerDown={startResize} />
      <div className="session-sidebar-inner">
        <header className="session-header">
          <span className="session-ticket">{worker.ticket}</span>
          {worker.kind ? <StatusBadge compact label={worker.kind} state="neutral" /> : null}
          {worker.role ? <StatusBadge compact label={worker.role} state="neutral" /> : null}
          {worker.model ? <StatusBadge compact label={worker.model} state="faint" /> : null}
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
        <InspectableSessionTab
          showComposer={false}
          ticket={worker.ticket}
          archivedAt={worker.archivedAt}
        />
      </div>
    </aside>
  );
}
