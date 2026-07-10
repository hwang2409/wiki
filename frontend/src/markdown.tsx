import { Children, isValidElement, useMemo, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { MarkdownPre } from "./shiki";
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
import type { NoteSummary } from "./types";

type MdNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
  data?: {
    hName?: string;
    hProperties?: Record<string, unknown>;
  };
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
      nodes.push({
        type: "link",
        url: "#",
        data: {
          hProperties: { className: "internal-link", "data-wikilink": target }
        },
        children: [{ type: "text", value: alias || target }]
      });
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

function createComponents(
  resolve: WikilinkResolver,
  onOpenNote: (path: string) => void,
  onCreateNote?: (target: string) => void
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
    blockquote: MarkdownBlockquote,
    pre: MarkdownPre
  };
}

export function ObsidianMarkdown({
  content,
  notes,
  onOpenNote,
  onCreateNote
}: {
  content: string;
  notes: NoteSummary[];
  onOpenNote: (path: string) => void;
  onCreateNote?: (target: string) => void;
}) {
  const prepared = useMemo(() => prepareMarkdown(content), [content]);
  const components = useMemo(
    () =>
      createComponents((target) => resolveWikilink(notes, target), onOpenNote, onCreateNote),
    [notes, onOpenNote, onCreateNote]
  );

  return (
    <ReactMarkdown
      components={components}
      remarkPlugins={[remarkGfm, remarkObsidianInline, remarkBreaks]}
    >
      {prepared}
    </ReactMarkdown>
  );
}
