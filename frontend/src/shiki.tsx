import { Children, isValidElement, useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { BundledLanguage, BundledTheme, Highlighter } from "shiki";
import { createHighlighter } from "shiki";
import { DEFAULT_THEME, normalizeTheme, type ThemeId } from "./themes";

const APP_THEME_TO_SHIKI: Record<ThemeId, BundledTheme> = {
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
  diff: "diff",
  patch: "diff",
};

function normalizeLang(lang: string | null | undefined): BundledLanguage | null {
  if (!lang) return null;
  return LANG_ALIAS[lang.trim().toLowerCase()] ?? null;
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
  let pending = loadingThemes.get(theme);
  if (!pending) {
    pending = h.loadTheme(theme).then(() => {
      loadedThemes.add(theme);
      loadingThemes.delete(theme);
    });
    loadingThemes.set(theme, pending);
  }
  await pending;
}

async function ensureLang(h: Highlighter, lang: BundledLanguage): Promise<void> {
  if (loadedLangs.has(lang)) return;
  let pending = loadingLangs.get(lang);
  if (!pending) {
    pending = h.loadLanguage(lang).then(() => {
      loadedLangs.add(lang);
      loadingLangs.delete(lang);
    });
    loadingLangs.set(lang, pending);
  }
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
    <pre className={wrapperClass} data-lang={normalized ?? undefined}>
      <code>{code}</code>
    </pre>
  );
}

function textFromReactNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromReactNode).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return textFromReactNode(node.props.children);
  }
  return "";
}

export function MarkdownPre({
  children,
  className: preClassName,
  ...rest
}: {
  children?: ReactNode;
  className?: string;
} & React.HTMLAttributes<HTMLPreElement>) {
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
    return <ShikiCode className="markdown-code-block" code={code} lang={lang} />;
  }
  return (
    <pre className={preClassName} {...rest}>
      {children}
    </pre>
  );
}

