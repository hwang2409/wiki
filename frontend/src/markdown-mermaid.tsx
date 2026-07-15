import { useEffect, useId, useState } from "react";
import { useCurrentTheme } from "./shiki";

function cssVariable(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function mermaidThemeVariables() {
  return {
    background: cssVariable("--background-primary"),
    primaryColor: cssVariable("--background-secondary"),
    primaryTextColor: cssVariable("--text-normal"),
    primaryBorderColor: cssVariable("--background-modifier-border"),
    lineColor: cssVariable("--text-muted"),
    secondaryColor: cssVariable("--background-primary-alt"),
    tertiaryColor: cssVariable("--background-primary"),
    secondaryTextColor: cssVariable("--text-normal"),
    tertiaryTextColor: cssVariable("--text-normal"),
    fontFamily: cssVariable("--font-interface") || "sans-serif"
  };
}

export function MermaidBlock({ source }: { source: string }) {
  const theme = useCurrentTheme();
  const reactId = useId();
  const [html, setHtml] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const id = `wiki-note-mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    setHtml("");
    setError(null);
    void import("mermaid").then(async ({ default: mermaid }) => {
      try {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          themeVariables: mermaidThemeVariables()
        });
        const rendered = await mermaid.render(id, source);
        if (!cancelled) setHtml(rendered.svg);
      } catch (reason) {
        if (!cancelled) {
          setHtml("");
          setError(reason instanceof Error ? reason.message : "Mermaid could not render this source.");
        }
      }
    });
    return () => {
      cancelled = true;
    };
  }, [reactId, source, theme]);

  if (error) {
    return (
      <div className="markdown-mermaid-error" role="alert">
        <div>{error}</div>
        <pre><code>{source}</code></pre>
      </div>
    );
  }
  if (!html) return <div className="markdown-mermaid-loading">Rendering diagram…</div>;
  return <div className="markdown-mermaid" dangerouslySetInnerHTML={{ __html: html }} />;
}
