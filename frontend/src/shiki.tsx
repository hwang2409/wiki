import { useEffect, useState } from "react";
import type { BundledLanguage, BundledTheme, Highlighter } from "shiki";
import { createHighlighter } from "shiki";
import { DEFAULT_THEME, normalizeTheme, type ThemeId } from "./themes";

const APP_THEME_TO_SHIKI: Record<ThemeId, BundledTheme> = {
  opencode: "everforest-dark",
  "mono-light": "github-light",
  "mono-dark": "github-dark",
  "gruvbox-light": "gruvbox-light-medium",
  "gruvbox-dark": "gruvbox-dark-medium",
  "vscode-dark-plus": "dark-plus",
  "solarized-light": "solarized-light",
  "solarized-dark": "solarized-dark",
  dracula: "dracula",
  nord: "nord",
  "one-dark": "one-dark-pro",
  "tokyo-night": "tokyo-night",
  "catppuccin-mocha": "catppuccin-mocha",
};

const LANG_ALIAS: Record<string, BundledLanguage> = {
  bash: "bash",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  python: "python",
  py: "python",
  typescript: "typescript",
  ts: "typescript",
  tsx: "tsx",
  javascript: "javascript",
  js: "javascript",
  jsx: "jsx",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  markdown: "markdown",
  md: "markdown",
  sql: "sql",
  go: "go",
  golang: "go",
  rust: "rust",
  rs: "rust",
  css: "css",
  html: "html",
  xml: "xml",
  diff: "diff",
  patch: "diff",
  c: "c",
  h: "c",
  cpp: "cpp",
  hpp: "cpp",
  java: "java",
  ruby: "ruby",
  rb: "ruby",
  php: "php",
  dockerfile: "docker",
};

function normalizeLang(lang: string | null | undefined): BundledLanguage | null {
  if (!lang) return null;
  return LANG_ALIAS[lang.trim().toLowerCase()] ?? null;
}

export function languageForPath(path: string): BundledLanguage | null {
  const filename = path.split("/").pop()?.toLowerCase() ?? "";
  if (filename === "dockerfile") return "docker";
  return normalizeLang(filename.split(".").pop() ?? null);
}

function resolveShikiTheme(appTheme: ThemeId): BundledTheme {
  return APP_THEME_TO_SHIKI[appTheme] ?? "github-light";
}

let highlighterPromise: Promise<Highlighter> | null = null;
const loadedThemes = new Set<BundledTheme>();
const loadedLangs = new Set<BundledLanguage>();
const loadingThemes = new Map<BundledTheme, Promise<void>>();
const loadingLangs = new Map<BundledLanguage, Promise<void>>();

async function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({ themes: [], langs: [] });
  }
  return highlighterPromise;
}

async function ensureTheme(h: Highlighter, theme: BundledTheme): Promise<void> {
  if (loadedThemes.has(theme)) return;
  const existing = loadingThemes.get(theme);
  if (existing) {
    await existing;
    return;
  }
  const pending: Promise<void> = h.loadTheme(theme).then(() => {
    loadedThemes.add(theme);
    loadingThemes.delete(theme);
  });
  loadingThemes.set(theme, pending);
  await pending;
}

async function ensureLang(h: Highlighter, lang: BundledLanguage): Promise<void> {
  if (loadedLangs.has(lang)) return;
  const existing = loadingLangs.get(lang);
  if (existing) {
    await existing;
    return;
  }
  const pending: Promise<void> = h.loadLanguage(lang).then(() => {
    loadedLangs.add(lang);
    loadingLangs.delete(lang);
  });
  loadingLangs.set(lang, pending);
  await pending;
}

export async function highlightToHtml(
  code: string,
  lang: string | null | undefined,
  appTheme: ThemeId
): Promise<string | null> {
  const normalized = normalizeLang(lang);
  if (!normalized) return null;
  const theme = resolveShikiTheme(appTheme);
  try {
    const h = await getHighlighter();
    await Promise.all([ensureTheme(h, theme), ensureLang(h, normalized)]);
    return h.codeToHtml(code, { lang: normalized, theme });
  } catch {
    return null;
  }
}

export type TokenLine = Array<{ content: string; color?: string }>;

const MAX_TOKENIZE_BYTES = 64 * 1024;
const MAX_TOKENIZE_LINES = 400;

// Token-level highlighting for transcript surfaces (command titles, diff
// lines, polished output). Returns per-line token runs instead of Shiki's
// pre/code HTML so callers control the DOM — and the wiki59 `.shiki-block`
// census stays scoped to fenced markdown blocks.
export async function highlightToTokenLines(
  code: string,
  lang: string | null | undefined,
  appTheme: ThemeId,
): Promise<TokenLine[] | null> {
  const normalized = normalizeLang(lang);
  if (!normalized) return null;
  if (code.length > MAX_TOKENIZE_BYTES || code.split("\n").length > MAX_TOKENIZE_LINES) return null;
  const theme = resolveShikiTheme(appTheme);
  try {
    const h = await getHighlighter();
    await Promise.all([ensureTheme(h, theme), ensureLang(h, normalized)]);
    const { tokens } = h.codeToTokens(code, { lang: normalized, theme });
    return tokens.map((line) => line.map((token) => ({ content: token.content, color: token.color })));
  } catch {
    return null;
  }
}

export function useHighlightTokenLines(code: string, lang: string | null | undefined): TokenLine[] | null {
  const theme = useCurrentTheme();
  const normalized = normalizeLang(lang);
  const [lines, setLines] = useState<TokenLine[] | null>(null);
  useEffect(() => {
    if (!normalized) {
      setLines(null);
      return;
    }
    let cancelled = false;
    highlightToTokenLines(code, normalized, theme).then((result) => {
      if (!cancelled) setLines(result);
    });
    return () => {
      cancelled = true;
    };
  }, [code, normalized, theme]);
  return normalized ? lines : null;
}

export function TokenizedLine({ tokens, fallback }: { tokens: TokenLine | null | undefined; fallback: string }) {
  if (!tokens) return <>{fallback}</>;
  return (
    <>
      {tokens.map((token, index) => (
        <span key={index} style={token.color ? { color: token.color } : undefined}>{token.content}</span>
      ))}
    </>
  );
}

// One-line-or-few inline highlight (no block chrome, no backgrounds): the
// bash `$ command` title, polished output bodies. Falls back to plain text
// until tokens resolve — content identical either way.
export function HighlightedCode({
  code,
  lang,
  lineNumbers = false,
  className,
}: {
  code: string;
  lang: string | null | undefined;
  lineNumbers?: boolean;
  className?: string;
}) {
  const tokenLines = useHighlightTokenLines(code, lang);
  const plainLines = code.split("\n");
  return (
    <span className={`syntax-inline${className ? ` ${className}` : ""}`} data-lang={normalizeLang(lang) ?? undefined}>
      {plainLines.map((line, index) => (
        <span className="syntax-inline-line" key={index}>
          {index > 0 ? "\n" : null}
          {lineNumbers ? (
            <span aria-hidden="true" className="syntax-inline-gutter tabular-nums">{index + 1}</span>
          ) : null}
          <TokenizedLine fallback={line} tokens={tokenLines?.[index]} />
        </span>
      ))}
    </span>
  );
}

export function useCurrentTheme(): ThemeId {
  const [theme, setTheme] = useState<ThemeId>(() => {
    if (typeof document === "undefined") return DEFAULT_THEME;
    return normalizeTheme(document.documentElement.dataset.theme ?? null);
  });
  useEffect(() => {
    const el = document.documentElement;
    const observer = new MutationObserver(() => {
      setTheme(normalizeTheme(el.dataset.theme ?? null));
    });
    observer.observe(el, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

export function ShikiCode({
  code,
  lang,
  className,
  transparent = false,
}: {
  code: string;
  lang: string | null | undefined;
  className?: string;
  transparent?: boolean;
}) {
  const theme = useCurrentTheme();
  const normalized = normalizeLang(lang);
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    if (!normalized) {
      setHtml(null);
      return;
    }
    let cancelled = false;
    highlightToHtml(code, normalized, theme).then((result) => {
      if (!cancelled) setHtml(result);
    });
    return () => {
      cancelled = true;
    };
  }, [code, normalized, theme]);

  const wrapperClass = [
    "shiki-block",
    transparent ? "is-transparent" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  if (html) {
    return (
      <div
        className={wrapperClass}
        data-lang={normalized ?? undefined}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }
  return (
    <div className={wrapperClass} data-lang={normalized ?? undefined}>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}
