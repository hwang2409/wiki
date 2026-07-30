import { Children, isValidElement, lazy, Suspense, useEffect, useMemo, useState } from "react";
import type { MouseEvent, ReactNode, TableHTMLAttributes } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { ArtifactLightbox } from "./artifact-detail/lightbox";
import { CopyPill } from "./copy-button";
import { ShikiCode } from "./shiki";
import {
  AlertTriangle,
  Bug,
  Check,
  CheckCircle2,
  ChevronDown,
  ClipboardList,
  Flame,
  HelpCircle,
  Info,
  List,
  Pencil,
  Quote,
  X,
  Zap
} from "lucide-react";
import { externalLinkProps, isExternalHttpUrl } from "./external-links";
import { GhPreviewCard, isGitHubPreviewUrl } from "./github-preview";
import type { NoteSummary } from "./types";

type MdNode = {
  type: string;
  value?: string;
  url?: string;
  alt?: string;
  children?: MdNode[];
  data?: {
    hName?: string;
    hProperties?: Record<string, unknown>;
  };
};

type HtmlNode = {
  type: string;
  value?: string;
  children?: HtmlNode[];
  tagName?: string;
  properties?: Record<string, unknown>;
};

const calloutTypes: Record<string, typeof Pencil> = {
  note: Pencil,
  info: Info,
  todo: CheckCircle2,
  abstract: ClipboardList,
  summary: ClipboardList,
  tldr: ClipboardList,
  tip: Flame,
  hint: Flame,
  important: Flame,
  success: Check,
  check: Check,
  done: Check,
  question: HelpCircle,
  help: HelpCircle,
  faq: HelpCircle,
  warning: AlertTriangle,
  caution: AlertTriangle,
  attention: AlertTriangle,
  failure: X,
  fail: X,
  missing: X,
  danger: Zap,
  error: Zap,
  bug: Bug,
  example: List,
  quote: Quote,
  cite: Quote
};

const calloutMarkerPattern = /^\[!([a-z-]+)\]([+-])?[ \t]*/i;
const wikiStylesPattern = /\n*```wiki-styles\n[\s\S]*?\n```\s*$/i;

function stripBlockComments(content: string) {
  const lines = content.split("\n");
  const kept: string[] = [];
  let inFence = false;
  let inComment = false;

  for (const line of lines) {
    if (!inComment && /^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      kept.push(line);
      continue;
    }
    if (inFence) {
      kept.push(line);
      continue;
    }
    if (!inComment) {
      if (/^\s*%%\s*$/.test(line)) {
        inComment = true;
        continue;
      }
      kept.push(line);
    } else if (/^\s*%%\s*$/.test(line)) {
      inComment = false;
    }
  }

  return kept.join("\n");
}

export function prepareMarkdown(content: string) {
  return stripBlockComments(content.replace(wikiStylesPattern, ""));
}

export function rehypeEscapeRawHtml() {
  return (tree: HtmlNode) => {
    function textContent(node: HtmlNode): string {
      if (node.type === "text") return node.value ?? "";
      return node.children?.map(textContent).join("") ?? "";
    }

    function escapeNode(node: HtmlNode): HtmlNode {
      if (node.type === "raw") {
        return {
          type: "element",
          tagName: "code",
          properties: { className: ["transcript-raw-html"] },
          children: [{ type: "text", value: node.value ?? "" }]
        };
      }

      const className = node.properties?.className;
      const classes = Array.isArray(className) ? className : typeof className === "string" ? className.split(/\s+/) : [];
      if (node.type === "element" && classes.some((name) => ["math-inline", "math-display", "language-math"].includes(name))) {
        return {
          type: "element",
          tagName: "code",
          properties: { className: ["transcript-raw-html"] },
          children: [{ type: "text", value: textContent(node) }]
        };
      }

      if (node.children) {
        node.children = node.children.map(escapeNode);
      }
      return node;
    }

    escapeNode(tree);
  };
}

function isFenceLine(line: string, fence: "`" | "~" | null) {
  const match = /^(\s*)([`~]{3,})(.*)$/.exec(line);
  if (!match) return null;

  const marker = match[2][0] as "`" | "~";
  if (fence && marker !== fence) return null;
  return { marker, length: match[2].length };
}

function matchOrderedListLine(line: string) {
  return /^(\d+)\.\s+/.exec(line);
}

function isOrderedListLine(line: string) {
  return matchOrderedListLine(line) !== null;
}

function isBulletListLine(line: string) {
  return /^\s*[-+*]\s+/.test(line);
}

function needsOrderedListSeparator(line: string) {
  return Number(matchOrderedListLine(line)?.[1] ?? 0) > 1;
}

function isNestedListLine(line: string) {
  return /^ {2}(?:[-+*]|\d+\.)\s+/.test(line);
}

function isTableDelimiterLine(line: string) {
  return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(line);
}

function isTableStartLine(line: string, nextLine?: string) {
  return Boolean(nextLine && line.includes("|") && isTableDelimiterLine(nextLine));
}

function isTranscriptTextLine(line: string) {
  return !isOrderedListLine(line) && !isBulletListLine(line) && !isTableDelimiterLine(line) && !line.includes("|");
}

export function prepareTranscriptMarkdown(content: string) {
  const lines = content.split(/\r?\n/);
  const output: string[] = [];
  let fence: "`" | "~" | null = null;
  let fenceLength = 0;
  let previousWasText = false;
  let tableBlock = false;
  let orderedContext = false;

  for (let index = 0; index < lines.length; index += 1) {
    const originalLine = lines[index];
    const nextLine = lines[index + 1];
    let line = originalLine;

    const fenceMatch = fence ? isFenceLine(line, fence) : null;
    if (fenceMatch && fenceMatch.length >= fenceLength) {
      output.push(line);
      fence = null;
      fenceLength = 0;
      previousWasText = false;
      continue;
    }

    if (fence) {
      output.push(line);
      continue;
    }

    const openingFence = isFenceLine(line, null);
    if (openingFence) {
      output.push(line);
      fence = openingFence.marker;
      fenceLength = openingFence.length;
      previousWasText = false;
      continue;
    }

    if (tableBlock) {
      output.push(line);
      previousWasText = false;
      if (line.trim() === "") {
        tableBlock = false;
      }
      continue;
    }

    if (line.trim() === "") {
      output.push(line);
      previousWasText = false;
      continue;
    }

    if (previousWasText && isTableStartLine(line, nextLine)) {
      output.push("");
    }

    if (orderedContext && isNestedListLine(line)) {
      line = `  ${line}`;
    }

    if (isTableStartLine(line, nextLine)) {
      output.push(line);
      tableBlock = true;
      previousWasText = false;
      continue;
    }

    if (previousWasText && needsOrderedListSeparator(line)) {
      output.push("");
    }

    output.push(line);
    previousWasText = isTranscriptTextLine(line);

    if (isOrderedListLine(line)) {
      orderedContext = true;
    } else if (!line.startsWith(" ") && !line.includes("|") && !isBulletListLine(line) && !isTableDelimiterLine(line)) {
      orderedContext = false;
    }
  }

  return output.join("\n");
}

export type NoteProperties = Array<[string, string | string[]]>;

const frontmatterPattern = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

function stripQuotes(value: string) {
  return value.replace(/^["']|["']$/g, "");
}

export function splitFrontmatter(content: string): {
  body: string;
  properties: NoteProperties | null;
} {
  const match = content.match(frontmatterPattern);
  if (!match) return { body: content, properties: null };

  const properties: NoteProperties = [];
  for (const line of match[1].split("\n")) {
    if (/^\s/.test(line)) continue;
    const divider = line.indexOf(":");
    if (divider === -1) continue;
    const key = line.slice(0, divider).trim();
    const raw = line.slice(divider + 1).trim();
    if (!key) continue;

    if (raw.startsWith("[") && raw.endsWith("]")) {
      properties.push([
        key,
        raw
          .slice(1, -1)
          .split(",")
          .map((item) => stripQuotes(item.trim()))
          .filter(Boolean)
      ]);
    } else {
      properties.push([key, stripQuotes(raw)]);
    }
  }

  return { body: content.slice(match[0].length), properties };
}

export function stripLeadingTitle(content: string, title: string) {
  const lines = content.split("\n");
  const firstContentLine = lines.findIndex((line) => line.trim() !== "");
  if (firstContentLine === -1) return content;

  const heading = lines[firstContentLine].trim();
  if (heading.replace(/^#\s+/, "") === title.trim() && heading.startsWith("# ")) {
    return lines.slice(firstContentLine + 1).join("\n").replace(/^\n+/, "");
  }
  return content;
}

const inlinePattern =
  /%%[\s\S]*?%%|==([^=\n]+)==|(!?)\[\[([^\][\n|]+?)(?:\|([^\][\n]+?))?\]\]|(^|[\s(])#([A-Za-z][\w/-]*)|\[(P\d)\]/g;

const vaultImagePattern = /\.(?:png|jpe?g|gif|webp|svg)$/i;

function isVaultImagePath(value: string) {
  return vaultImagePattern.test(value.split(/[?#]/, 1)[0]);
}

function splitInline(value: string): MdNode[] {
  const nodes: MdNode[] = [];
  let last = 0;
  let matched = false;
  inlinePattern.lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = inlinePattern.exec(value)) !== null) {
    matched = true;
    if (match.index > last) {
      nodes.push({ type: "text", value: value.slice(last, match.index) });
    }

    if (match[1] !== undefined) {
      nodes.push({
        type: "strong",
        data: { hName: "mark" },
        children: [{ type: "text", value: match[1] }]
      });
    } else if (match[3] !== undefined) {
      const target = match[3].trim();
      const alias = match[4]?.trim();
      if (match[2] === "!" && isVaultImagePath(target)) {
        const width = alias && /^\d+$/.test(alias) ? Number(alias) : undefined;
        nodes.push({
          type: "image",
          url: target,
          alt: target,
          data: {
            hProperties: {
              ...(width ? { "data-obsidian-width": width } : {}),
              "data-obsidian-embed": true
            }
          }
        });
      } else {
        nodes.push({
          type: "link",
          url: "#",
          data: {
            hProperties: { className: "internal-link", "data-wikilink": target }
          },
          children: [{ type: "text", value: alias || target }]
        });
      }
    } else if (match[6] !== undefined) {
      if (match[5]) nodes.push({ type: "text", value: match[5] });
      nodes.push({
        type: "link",
        url: "#",
        data: { hProperties: { className: "tag" } },
        children: [{ type: "text", value: `#${match[6]}` }]
      });
    } else if (match[7] !== undefined) {
      nodes.push({
        type: "strong",
        data: {
          hName: "span",
          hProperties: {
            className: `priority-badge priority-${match[7].toLowerCase()}`
          }
        },
        children: [{ type: "text", value: match[7] }]
      });
    }

    last = match.index + match[0].length;
  }

  if (!matched) return [{ type: "text", value }];
  if (last < value.length) nodes.push({ type: "text", value: value.slice(last) });
  return nodes;
}

function remarkObsidianInline() {
  return (tree: MdNode) => {
    function walk(node: MdNode) {
      if (!node.children) return;
      if (node.type === "link" || node.type === "linkReference") return;

      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === "text" && typeof child.value === "string") {
          next.push(...splitInline(child.value));
        } else {
          walk(child);
          next.push(child);
        }
      }
      node.children = next;
    }

    walk(tree);
  };
}

function textFromReactNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromReactNode).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return textFromReactNode(node.props.children);
  }
  return "";
}

function isParagraphElement(node: ReactNode): node is ReactNode & { props: { children?: ReactNode } } {
  return isValidElement(node) && node.type === "p";
}

function splitTitleLine(paragraphChildren: ReactNode): {
  titleNodes: ReactNode[];
  rest: ReactNode[];
} {
  const items = Children.toArray(paragraphChildren);

  for (let index = 0; index < items.length; index += 1) {
    const child = items[index];
    if (isValidElement(child) && child.type === "br") {
      return { titleNodes: items.slice(0, index), rest: items.slice(index + 1) };
    }
    if (typeof child === "string" && child.includes("\n")) {
      const newline = child.indexOf("\n");
      const before = child.slice(0, newline);
      const after = child.slice(newline + 1);
      return {
        titleNodes: [...items.slice(0, index), before],
        rest: [after, ...items.slice(index + 1)].filter(
          (node) => node !== ""
        )
      };
    }
  }

  return { titleNodes: items, rest: [] };
}

function Callout({
  kind,
  fold,
  title,
  body
}: {
  kind: string;
  fold: "+" | "-" | null;
  title: string;
  body: ReactNode[];
}) {
  const [open, setOpen] = useState(fold !== "-");
  const Icon = calloutTypes[kind] ?? calloutTypes.note;
  const foldable = fold !== null;
  const hasBody = body.length > 0;

  return (
    <div
      className={`callout${foldable ? " is-collapsible" : ""}${open ? "" : " is-collapsed"}`}
      data-callout={kind}
    >
      <div
        className="callout-title"
        onClick={foldable ? () => setOpen((value) => !value) : undefined}
        role={foldable ? "button" : undefined}
      >
        <div className="callout-icon">
          <Icon size={16} />
        </div>
        <div className="callout-title-inner">{title}</div>
        {foldable ? (
          <div className="callout-fold">
            <ChevronDown size={16} />
          </div>
        ) : null}
      </div>
      {hasBody ? (
        <div className={`callout-content-wrap${open ? " is-open" : ""}`}>
          <div className="callout-content">{body}</div>
        </div>
      ) : null}
    </div>
  );
}

function MarkdownBlockquote({ children }: { children?: ReactNode; node?: unknown }) {
  const items = Children.toArray(children);
  const firstParagraphIndex = items.findIndex(isParagraphElement);
  const firstParagraph = firstParagraphIndex === -1 ? null : items[firstParagraphIndex];

  const markerMatch = firstParagraph
    ? textFromReactNode(firstParagraph).match(calloutMarkerPattern)
    : null;

  if (!firstParagraph || !markerMatch || !isValidElement<{ children?: ReactNode }>(firstParagraph)) {
    return <blockquote>{children}</blockquote>;
  }

  const kind = markerMatch[1].toLowerCase();
  const fold = (markerMatch[2] as "+" | "-" | undefined) ?? null;
  const defaultTitle = kind.charAt(0).toUpperCase() + kind.slice(1);

  const { titleNodes, rest } = splitTitleLine(firstParagraph.props.children);
  const titleLine = textFromReactNode(titleNodes).replace(calloutMarkerPattern, "").trim();

  const body: ReactNode[] = [];
  if (rest.length > 0) {
    body.push(<p key="callout-lead">{rest}</p>);
  }
  body.push(...items.slice(firstParagraphIndex + 1));

  return (
    <Callout
      body={body}
      fold={fold}
      kind={kind}
      title={titleLine || defaultTitle}
    />
  );
}

export type WikilinkResolver = (target: string) => string | null;

export function resolveWikilink(notes: NoteSummary[], target: string): string | null {
  const needle = target.trim().toLowerCase();
  const withoutExtension = needle.replace(/\.md$/, "");

  for (const note of notes) {
    const path = note.path.toLowerCase();
    const basename = path.split("/").pop()?.replace(/\.md$/, "") ?? "";
    if (
      basename === withoutExtension ||
      path === needle ||
      path.replace(/\.md$/, "") === withoutExtension ||
      note.title.toLowerCase() === needle
    ) {
      return note.path;
    }
  }

  return null;
}

type MarkdownLinkProps = {
  children?: ReactNode;
  className?: string;
  href?: string;
  node?: unknown;
  "data-wikilink"?: string;
};

export function MarkdownPre({
  children,
  ...rest
}: React.HTMLAttributes<HTMLPreElement>) {
  const items = Children.toArray(children);
  const child = items.length === 1 ? items[0] : null;
  if (
    child &&
    isValidElement<{ className?: string; children?: ReactNode }>(child) &&
    child.type === "code"
  ) {
    const langMatch = /language-([\w-]+)/.exec(child.props.className ?? "");
    const lang = langMatch?.[1] ?? null;
    const code = textFromReactNode(child.props.children).replace(/\n$/, "");
    if (lang === "mermaid") {
      return (
        <Suspense fallback={<div className="markdown-mermaid-loading">Rendering diagram…</div>}>
          <LazyMermaidBlock source={code} />
        </Suspense>
      );
    }
    return (
      <div className="markdown-code-block-wrap" data-lang={lang ?? undefined}>
        <ShikiCode className="markdown-code-block" code={code} lang={lang} />
        <CopyPill className="markdown-code-copy" getText={() => code} />
      </div>
    );
  }
  return <pre {...rest}>{children}</pre>;
}

const LazyMermaidBlock = lazy(() =>
  import("./markdown-mermaid").then(({ MermaidBlock }) => ({ default: MermaidBlock }))
);

function decodeAssetPath(value: string) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function normalizeAssetPath(value: string, base: string[] = []) {
  const parts = value.replaceAll("\\", "/").split("/");
  const resolved = [...base];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (resolved.length > 0 && resolved[resolved.length - 1] !== "..") resolved.pop();
      else resolved.push("..");
    } else {
      resolved.push(part);
    }
  }
  return resolved.length > 0 ? resolved.join("/") : null;
}

function vaultAssetUrl(path: string) {
  return `/api/vault/assets/${path.split("/").map(encodeURIComponent).join("/")}`;
}

function assetCandidates(src: string, notePath?: string) {
  if (/^[a-z][a-z\d+.-]*:/i.test(src) || src.startsWith("//")) return [];
  const decoded = decodeAssetPath(src.split(/[?#]/, 1)[0]);
  if (decoded.includes("\0") || decoded.startsWith("/")) return [];
  if (!isVaultImagePath(decoded)) return [];
  const rootPath = normalizeAssetPath(decoded);
  const notePathCandidate = notePath
    ? normalizeAssetPath(decoded, notePath.split("/").slice(0, -1))
    : null;
  return [notePathCandidate, rootPath].filter(
    (candidate, index, candidates): candidate is string =>
      Boolean(candidate) && candidates.indexOf(candidate) === index,
  );
}

type MarkdownImageProps = {
  alt?: string;
  className?: string;
  "data-obsidian-width"?: number | string;
  node?: unknown;
  notePath?: string;
  src?: string;
};

function nodeProperties(node: unknown): Record<string, unknown> {
  if (!node || typeof node !== "object") return {};
  const typedNode = node as {
    data?: { hProperties?: Record<string, unknown> };
    properties?: Record<string, unknown>;
  };
  return typedNode.data?.hProperties ?? typedNode.properties ?? {};
}

type AssetMeta = {
  width: number;
  height: number;
  previewBase64: string | null;
};

// Shared cache: results are cached forever, in-flight fetches are shared, and
// the underlying fetch is NEVER aborted from a consumer's cleanup — each
// consumer manages its own cancellation via a cancelled flag. This avoids
// the round-2 bug where the first consumer's unmount cancelled the request
// for every other consumer waiting on the same asset.
const assetMetaCache = new Map<string, AssetMeta | null>();
const assetMetaPending = new Map<string, Promise<AssetMeta | null>>();

function fetchAssetMeta(path: string): Promise<AssetMeta | null> {
  const cached = assetMetaCache.get(path);
  if (cached !== undefined) return Promise.resolve(cached);
  const pending = assetMetaPending.get(path);
  if (pending) return pending;
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const request = fetch(`/api/vault/asset-meta/${encoded}`)
    .then(async (response) => {
      if (!response.ok) return null;
      const body = await response.json();
      const meta: AssetMeta = {
        width: Number(body.width) || 0,
        height: Number(body.height) || 0,
        previewBase64: typeof body.preview_base64 === "string" ? body.preview_base64 : null,
      };
      return meta;
    })
    .then((meta) => {
      assetMetaCache.set(path, meta);
      assetMetaPending.delete(path);
      return meta;
    })
    .catch(() => {
      assetMetaPending.delete(path);
      assetMetaCache.set(path, null);
      return null;
    });
  assetMetaPending.set(path, request);
  return request;
}

export function seedAssetMetaCache(entries: Record<string, {
  width: number;
  height: number;
  preview_base64?: string | null;
}> | undefined | null): void {
  if (!entries) return;
  for (const [path, entry] of Object.entries(entries)) {
    if (!entry || typeof entry !== "object") continue;
    assetMetaCache.set(path, {
      width: Number(entry.width) || 0,
      height: Number(entry.height) || 0,
      previewBase64: typeof entry.preview_base64 === "string" ? entry.preview_base64 : null,
    });
  }
}

export function assetMetaFromCache(path: string): AssetMeta | null | undefined {
  return assetMetaCache.get(path);
}

function MarkdownImage({ alt, className, "data-obsidian-width": dataWidth, node, notePath, src }: MarkdownImageProps) {
  const candidates = src ? assetCandidates(src, notePath) : [];
  const [failed, setFailed] = useState(false);
  const activeCandidate = candidates.length > 0
    ? candidates[failed && candidates.length > 1 ? 1 : 0]
    : null;
  // Consult the cache SYNCHRONOUSLY during the first render so any dimensions
  // that arrived with the note payload (or a prior render) are on the frame
  // before React commits. Falls back to an async probe only if the cache is
  // empty for this asset — the async fetch is shared across every consumer
  // and never aborted by an individual consumer's cleanup.
  const cachedMeta = activeCandidate ? assetMetaFromCache(activeCandidate) : undefined;
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [meta, setMeta] = useState<AssetMeta | null>(cachedMeta ?? null);
  // Ratio is LOCKED once — either from meta arriving before image load, or
  // from the image's own naturalWidth/Height on load if meta hasn't shown
  // up yet. Late-arriving meta is used for the lightbox but never rewrites
  // the frame's aspect ratio, so a metadata race can't cause CLS.
  const [frameRatio, setFrameRatio] = useState<{ w: number; h: number } | null>(() =>
    cachedMeta && cachedMeta.width > 0 && cachedMeta.height > 0
      ? { w: cachedMeta.width, h: cachedMeta.height }
      : null,
  );
  const [previewMounted, setPreviewMounted] = useState(true);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  useEffect(() => {
    setFailed(false);
    setState("loading");
    setPreviewMounted(true);
    // Prime meta from cache on src/notePath change; not on activeCandidate
    // change so a fallback retry (candidates[0] -> candidates[1]) doesn't
    // undo itself in an infinite loop.
    const first = candidates.length > 0 ? candidates[0] : null;
    const preloaded = first ? assetMetaFromCache(first) : undefined;
    setMeta(preloaded ?? null);
    setFrameRatio(
      preloaded && preloaded.width > 0 && preloaded.height > 0
        ? { w: preloaded.width, h: preloaded.height }
        : null,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, notePath]);

  useEffect(() => {
    if (state !== "ready") return;
    const timer = window.setTimeout(() => setPreviewMounted(false), 380);
    return () => window.clearTimeout(timer);
  }, [state]);

  useEffect(() => {
    if (!activeCandidate) return;
    if (assetMetaFromCache(activeCandidate) !== undefined) return;
    let cancelled = false;
    fetchAssetMeta(activeCandidate).then((info) => {
      if (cancelled) return;
      setMeta(info);
      // Only lock the ratio from meta if we don't have one yet — otherwise
      // the ratio was captured from the image's naturalWidth/Height at load
      // time and we must NOT change it (that would be the CLS the race test
      // hunts for).
      if (info && info.width > 0 && info.height > 0) {
        setFrameRatio((prev) => prev ?? { w: info.width, h: info.height });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeCandidate]);

  const currentSrc = src && activeCandidate ? vaultAssetUrl(activeCandidate) : src;
  const widthValue = dataWidth ?? nodeProperties(node)["data-obsidian-width"];
  const explicitWidth = typeof widthValue === "number" ? widthValue : undefined;

  if (!currentSrc) return null;

  const knownRatio = frameRatio ? frameRatio.w / frameRatio.h : null;

  const frameStyle: React.CSSProperties = {};
  if (explicitWidth) frameStyle.width = `${explicitWidth}px`;
  if (frameRatio) frameStyle.aspectRatio = `${frameRatio.w} / ${frameRatio.h}`;

  const label = alt || src?.split(/[\\/]/).pop() || "Image";

  return (
    <>
      <button
        aria-label={`Open ${label} in fullscreen`}
        className={`markdown-image-frame${state === "ready" ? " is-loaded" : ""}${state === "error" ? " is-error" : ""}${knownRatio ? " has-known-ratio" : ""}`}
        onClick={() => {
          if (state === "ready") setLightboxOpen(true);
        }}
        style={frameStyle}
        type="button"
      >
        {previewMounted && meta?.previewBase64 ? (
          <img
            aria-hidden="true"
            alt=""
            className={`markdown-image-preview${state === "ready" ? " is-fading" : ""}`}
            decoding="sync"
            src={meta.previewBase64}
          />
        ) : state === "loading" ? (
          <span className="markdown-image-shimmer" aria-hidden="true" />
        ) : null}
        <img
          alt={alt ?? ""}
          className={className}
          decoding="async"
          height={meta?.height}
          loading="lazy"
          src={currentSrc}
          style={explicitWidth ? { width: `${explicitWidth}px` } : undefined}
          width={meta?.width}
          onError={() => {
            if (candidates.length > 1 && !failed) {
              setFailed(true);
              return;
            }
            // Only flip to the fallback overlay for vault-relative sources —
            // external URLs stay in the DOM so consumers can still inspect
            // them and let the browser render the native broken-image icon.
            if (candidates.length > 0) setState("error");
          }}
          onLoad={(event) => {
            const img = event.currentTarget;
            // Fallback ratio lock: if metadata hasn't shown up yet, use the
            // image's own naturalWidth/Height so the frame stays put when
            // meta arrives later.
            if (img.naturalWidth > 0 && img.naturalHeight > 0) {
              setFrameRatio((prev) => prev ?? { w: img.naturalWidth, h: img.naturalHeight });
            }
            setState("ready");
          }}
        />
        {state === "error" ? (
          <span className="markdown-image-error" role="img" aria-label={`Image failed to load: ${label}`}>
            image unavailable
          </span>
        ) : null}
      </button>
      {lightboxOpen ? (
        <ArtifactLightbox
          index={0}
          items={[{
            src: currentSrc,
            alt: label,
            caption: alt || label,
            width: meta?.width,
            height: meta?.height,
          }]}
          onClose={() => setLightboxOpen(false)}
          onIndexChange={() => undefined}
        />
      ) : null}
    </>
  );
}

export function MarkdownTable({ children, ...props }: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="markdown-table-scroll">
      <table {...props}>{children}</table>
    </div>
  );
}

function createComponents(
  resolve: WikilinkResolver,
  onOpenNote: (path: string) => void,
  onCreateNote?: (target: string) => void,
  notePath?: string
) {
  function MarkdownLink({ children, className, href, node: _node, ...props }: MarkdownLinkProps) {
    const wikilink = props["data-wikilink"];

    if (typeof wikilink === "string") {
      const resolved = resolve(wikilink);
      return (
        <a
          className={`internal-link${resolved ? "" : " is-unresolved"}`}
          href="#"
          title={resolved ? undefined : `Create "${wikilink}"`}
          onClick={(event: MouseEvent<HTMLAnchorElement>) => {
            event.preventDefault();
            if (resolved) {
              onOpenNote(resolved);
            } else {
              onCreateNote?.(wikilink);
            }
          }}
        >
          {children}
        </a>
      );
    }

    if (className?.includes("tag")) {
      return (
        <a className="tag" href="#" onClick={(event) => event.preventDefault()}>
          {children}
        </a>
      );
    }

    const isExternal = isExternalHttpUrl(href);
    if (isExternal) {
      if (typeof href === "string" && isGitHubPreviewUrl(href)) {
        return <GhPreviewCard url={href} />;
      }
      return (
        <a className="external-link" href={href} {...externalLinkProps(href)}>
          {children}
        </a>
      );
    }

    return (
      <a className={className} href={href}>
        {children}
      </a>
    );
  }

  return {
    a: MarkdownLink,
    img: (props: MarkdownImageProps) => <MarkdownImage {...props} notePath={notePath} />,
    blockquote: MarkdownBlockquote,
    pre: MarkdownPre,
    table: MarkdownTable
  };
}

export function ObsidianMarkdown({
  assetMeta,
  content,
  notes,
  notePath,
  onOpenNote,
  onCreateNote
}: {
  assetMeta?: Record<string, { width: number; height: number; preview_base64?: string | null }>;
  content: string;
  notes: NoteSummary[];
  notePath?: string;
  onOpenNote: (path: string) => void;
  onCreateNote?: (target: string) => void;
}) {
  // Seed the shared cache SYNCHRONOUSLY before ReactMarkdown renders, so
  // every MarkdownImage frame commits with correct width/height/preview on
  // its first render (no metadata race, no layout shift).
  if (assetMeta) {
    seedAssetMetaCache(assetMeta);
  }
  const prepared = useMemo(() => prepareMarkdown(content), [content]);
  const components = useMemo(
    () =>
      createComponents(
        (target) => resolveWikilink(notes, target),
        onOpenNote,
        onCreateNote,
        notePath
      ),
    [notes, notePath, onOpenNote, onCreateNote]
  );

  return (
    <ReactMarkdown
      components={components}
      rehypePlugins={[rehypeKatex]}
      remarkPlugins={[remarkGfm, remarkMath, remarkObsidianInline, remarkBreaks]}
    >
      {prepared}
    </ReactMarkdown>
  );
}
