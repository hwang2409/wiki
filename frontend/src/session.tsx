import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Bell,
  Bot,
  ChevronRight,
  CircleCheck,
  Eye,
  FileText,
  GitCommit,
  GitPullRequest,
  Hourglass,
  ListTodo,
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
import {
  cancelQueuedMessage,
  getAgentQueue,
  getAgentSession,
  getSkills,
  getSubagentSession,
  sendAgentMessage,
  uploadImage,
} from "./api";
import type { AgentSessionData, QueuedMessage, SessionEvent, SkillInfo, SubagentInfo } from "./api";
import { LoadingPlaceholder } from "./loading";

const POLL_MS = 2500;

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

function usePinnedScroll<T extends HTMLElement>(dep: unknown, resetKey: unknown) {
  const ref = useRef<T | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  // New target (ticket switch) always starts pinned at the bottom.
  useLayoutEffect(() => {
    pinnedRef.current = true;
  }, [resetKey]);

  // Pin before paint so an opened log never flashes at the top.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [dep]);

  // Content can grow after the effect (font swap, wrapping, expands) — re-pin on resize.
  useEffect(() => {
    const el = ref.current;
    const inner = innerRef.current;
    if (!el || !inner) return;
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(inner);
    return () => observer.disconnect();
  }, [dep]);

  const onScroll = (event: React.UIEvent<T>) => {
    const el = event.currentTarget;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };
  return { ref, innerRef, onScroll };
}

function formatTokens(tokens: number | null): string | null {
  if (!tokens) return null;
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M tok`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k tok`;
  return `${tokens} tok`;
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
        return <ImageChip key={i} num={imageNums[imgIndex] ?? 0} url={part.url} />;
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
}: {
  event: SessionEvent;
  onInspect?: (agentId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const tool = event.tool!;
  const Icon = ARCHETYPE_ICONS[tool.archetype] ?? Terminal;
  const summary = tool.summary || tool.input.split("\n")[0].slice(0, 120);
  const running = tool.output === null && tool.ok === null;
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
            <pre>{tool.input}</pre>
            {tool.output ? <pre className="session-tool-output">{tool.output}</pre> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

type EventGroup =
  | { kind: "message"; event: SessionEvent; key: number }
  | { kind: "activity"; events: SessionEvent[]; key: number };

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

function ImageChip({ url, num }: { url: string; num: number }) {
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
        rel="noopener noreferrer"
        target="_blank"
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

const sameEvents = (
  prev: { events: SessionEvent[]; onInspect?: (agentId: string) => void },
  next: { events: SessionEvent[]; onInspect?: (agentId: string) => void }
) =>
  prev.onInspect === next.onInspect &&
  prev.events.length === next.events.length &&
  prev.events.every((event, i) => event === next.events[i]);

function BashBlock({ event }: { event: SessionEvent }) {
  const bash = event.bash ?? { input: "", stdout: "", stderr: "" };
  const output = [bash.stdout, bash.stderr].filter(Boolean).join("\n");
  const canCollapse = output.length > 700 || output.split("\n").length > 14;
  const [open, setOpen] = useState(!canCollapse);
  return (
    <div className={`session-bash${open ? " is-open" : ""}${canCollapse ? " is-collapsible" : ""}`}>
      {bash.input ? (
        <div className="session-bash-command">
          <span className="session-bash-prompt">❯</span>
          <pre>{bash.input}</pre>
        </div>
      ) : null}
      {output ? (
        <div className={`session-collapsible session-bash-collapsible${open ? " is-open" : ""}`}>
          <div className="session-collapsible-inner">
            <div className="session-bash-body">
              {bash.stdout ? <pre className="session-bash-stdout">{bash.stdout}</pre> : null}
              {bash.stderr ? <pre className="session-bash-stderr">{bash.stderr}</pre> : null}
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

const MessageBlock = memo(function MessageBlock({
  event,
  imageNums,
}: {
  event: SessionEvent;
  imageNums?: number[];
}) {
  if (event.kind === "user") {
    return (
      <div className="session-user">
        <UserText imageNums={imageNums} text={event.text} />
      </div>
    );
  }
  if (event.kind === "terminal") {
    return <pre className="session-pane-log">{event.text}</pre>;
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
  return (
    <div className="session-assistant markdown-preview-view">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{event.text}</ReactMarkdown>
    </div>
  );
});

function ActivityGroupBase({
  events,
  onInspect,
}: {
  events: SessionEvent[];
  onInspect?: (agentId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const tools = events.filter((e) => e.kind === "tool");
  const thinking = events.filter((e) => e.kind === "thinking");
  const parts: string[] = [];
  if (tools.length) parts.push(`${tools.length} tool call${tools.length > 1 ? "s" : ""}`);
  if (thinking.length) parts.push(`${thinking.length} thinking`);
  const visible = events.filter((e) => e.kind === "tool" || e.text);
  return (
    <div className="session-activity">
      <button className="session-activity-head" type="button" onClick={() => setOpen(!open)}>
        <ChevronRight className={`collapse-icon${open ? "" : " is-collapsed"}`} size={12} />
        {parts.join(" · ") || "activity"}
      </button>
      <div className={`session-collapsible session-activity-collapsible${open ? " is-open" : ""}`}>
        <div className="session-collapsible-inner">
          <div className="session-activity-body">
          {visible.map((event, i) =>
            event.kind === "tool" ? (
              <ToolRow event={event} key={i} onInspect={onInspect} />
            ) : (
              <div className="session-thinking" key={i}>
                {event.text}
              </div>
            )
          )}
          </div>
        </div>
      </div>
    </div>
  );
}

const ActivityGroup = memo(ActivityGroupBase, sameEvents);

type SessionAcc = {
  format: string;
  path: string;
  tokens: number | null;
  base: number;
  events: SessionEvent[];
  subagents: SubagentInfo[];
  working: boolean;
};

function spliceSession(acc: SessionAcc | null, result: AgentSessionData): SessionAcc {
  const clientEnd = acc ? acc.base + acc.events.length : 0;
  if (!acc || result.path !== acc.path || result.from > clientEnd || result.from < acc.base) {
    return {
      format: result.format,
      path: result.path,
      tokens: result.tokens,
      base: result.from,
      events: result.events,
      subagents: result.subagents ?? [],
      working: result.working ?? false,
    };
  }
  // Delta: keep old event objects (memo identity), replace from the dirty point on.
  return {
    ...acc,
    tokens: result.tokens,
    subagents: result.subagents ?? acc.subagents,
    working: result.working ?? acc.working,
    events: acc.events.slice(0, result.from - acc.base).concat(result.events),
  };
}

export function SessionTab({
  ticket,
  tick,
  subagent,
  showComposer = true,
  onInspect,
}: {
  ticket: string;
  tick: number;
  subagent?: string;
  showComposer?: boolean;
  onInspect?: (agentId: string) => void;
}) {
  const [session, setSession] = useState<SessionAcc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef<SessionAcc | null>(null);
  sessionRef.current = session;
  const resetKey = `${ticket}:${subagent ?? ""}`;
  const { ref, innerRef, onScroll } = usePinnedScroll<HTMLDivElement>(session, resetKey);

  useEffect(() => {
    setSession(null);
    setError(null);
    sessionRef.current = null;
  }, [resetKey]);

  useEffect(() => {
    let ignore = false;
    const acc = sessionRef.current;
    const after = acc ? acc.base + acc.events.length : 0;
    (subagent
      ? getSubagentSession(ticket, subagent, after)
      : getAgentSession(ticket, after))
      .then((result) => {
        if (ignore) return;
        setSession((prev) => spliceSession(prev, result));
        setError(null);
      })
      .catch(() => {
        if (!ignore) setError("No session transcript found.");
      });
    return () => {
      ignore = true;
    };
  }, [resetKey, ticket, subagent, tick]);

  const groups = useMemo(
    () => groupEvents(session?.events ?? [], session?.base ?? 0),
    [session]
  );

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

  if (error && !session) return <div className="session-empty">{error}</div>;
  if (!session) {
    return (
      <div className="session-empty">
        <LoadingPlaceholder className="session-loading" lines={[82, 96, 74, 88]} />
      </div>
    );
  }

  const tokens = formatTokens(session.tokens);
  return (
    <>
      <div className="session-scroll" ref={ref} onScroll={onScroll}>
        <div className="session-scroll-inner" ref={innerRef}>
        {groups.map((group) =>
          group.kind === "activity" ? (
            <ActivityGroup events={group.events} key={group.key} onInspect={onInspect} />
          ) : (
            <MessageBlock
              event={group.event}
              imageNums={imageNumbers.get(group.event)}
              key={group.key}
            />
          )
        )}
      </div>
      </div>
      {subagent || !showComposer ? null : (
        <MessageComposer
          history={userHistory}
          runningSubagents={runningSubagents}
          thinking={session.working}
          ticket={ticket}
          tick={tick}
          onInspect={onInspect}
        />
      )}
      <div className="session-footer tabular-nums">
        {session.format} · {session.path.split("/").slice(-1)[0]}
        {tokens ? ` · ${tokens}` : ""}
      </div>
    </>
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
  ticket,
  tick,
  history = [],
  runningSubagents = [],
  thinking = false,
  onInspect,
}: {
  ticket: string;
  tick: number;
  history?: string[];
  runningSubagents?: SubagentInfo[];
  thinking?: boolean;
  onInspect?: (agentId: string) => void;
}) {
  const [text, setText] = useState("");
  const [queued, setQueued] = useState<QueuedMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [vimMode, setVimMode] = useState<"insert" | "normal" | "visual">("insert");
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
  const [caretPos, setCaretPos] = useState(0);
  const [overlayPos, setOverlayPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el || vimMode === "insert") {
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

  function insertNewline() {
    const el = inputRef.current;
    const at = el ? el.selectionStart : text.length;
    setText((current) => current.slice(0, at) + "\n" + current.slice(at));
    requestAnimationFrame(() => inputRef.current?.setSelectionRange(at + 1, at + 1));
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
    requestAnimationFrame(() => inputRef.current?.setSelectionRange(pos, pos));
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
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, Math.round(window.innerHeight * 0.4))}px`;
  }, [text]);

  function caret(): number {
    return inputRef.current?.selectionStart ?? 0;
  }

  // Block cursor: normal mode keeps a 1-char selection styled via ::selection.
  function setBlock(at: number) {
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      const start = Math.max(0, Math.min(at, Math.max(0, el.value.length - 1)));
      el.setSelectionRange(start, Math.min(start + 1, el.value.length));
      setCaretPos(start);
    });
  }

  function setInsertCaret(at: number) {
    requestAnimationFrame(() => {
      inputRef.current?.setSelectionRange(at, at);
      setCaretPos(at);
    });
  }

  function enterInsert(at: number) {
    setVimMode("insert");
    setInsertCaret(at);
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

  useEffect(() => {
    let ignore = false;
    getAgentQueue(ticket)
      .then((result) => {
        if (!ignore) setQueued(result.messages);
      })
      .catch(() => {});
    return () => {
      ignore = true;
    };
  }, [ticket, tick]);

  function setVisualSelection(anchor: number, head: number) {
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      const lo = Math.min(anchor, head);
      const hi = Math.min(el.value.length, Math.max(anchor, head) + 1);
      el.setSelectionRange(lo, hi);
      setCaretPos(head);
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
      await sendAgentMessage(ticket, value, mode);
      historyPosRef.current = null;
      setText("");
      setAttachments([]);
      if (mode === "on-idle") {
        const result = await getAgentQueue(ticket);
        setQueued(result.messages);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setBusy(false);
    }
  }

  async function cancel(index: number) {
    try {
      const result = await cancelQueuedMessage(ticket, index);
      setQueued(result.messages);
    } catch {
      /* queue changed under us — next tick refreshes */
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
          className={vimMode === "insert" ? undefined : "is-vim-normal"}
          spellCheck={false}
          placeholder={vimMode === "insert" ? "Enter sends now · Shift+Enter queues until idle · Esc = vim normal" : undefined}
          ref={inputRef}
          rows={2}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setMenuDismissed(false);
            setMenuIndex(0);
          }}
          onClick={(event) => setCaretPos(event.currentTarget.selectionStart)}
          onKeyUp={(event) => setCaretPos(event.currentTarget.selectionStart)}
          onSelect={(event) => setCaretPos(event.currentTarget.selectionStart)}
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
              if (event.key === "ArrowDown" || (event.key === "Tab" && !event.shiftKey)) {
                event.preventDefault();
                setMenuIndex((i) => (i + 1) % menuItems.length);
                return;
              }
              if (event.key === "ArrowUp" || (event.key === "Tab" && event.shiftKey)) {
                event.preventDefault();
                setMenuIndex((i) => (i - 1 + menuItems.length) % menuItems.length);
                return;
              }
              if (event.key === "Enter") {
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
          {vimMode === "insert" ? "-- INSERT --" : vimMode === "visual" ? "-- VISUAL --" : "-- NORMAL --"}
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
  refreshTick,
  onClose,
  onOpenAgent,
}: {
  worker: SidebarTarget;
  refreshTick: number;
  onClose: () => void;
  onOpenAgent: (ticket: string, panel?: "review") => void;
}) {
  const tick = usePollTick(refreshTick);
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
        <SessionTab showComposer={false} ticket={worker.ticket} tick={tick} />
      </div>
    </aside>
  );
}
