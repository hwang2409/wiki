import { useEffect, useRef, useState } from "react";

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const holder = document.createElement("textarea");
    holder.value = text;
    holder.setAttribute("readonly", "");
    holder.style.position = "fixed";
    holder.style.top = "-1000px";
    holder.style.opacity = "0";
    document.body.appendChild(holder);
    holder.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(holder);
    return ok;
  } catch {
    return false;
  }
}

export function CopyPill({
  getText,
  className,
  label = "copy",
  copiedLabel = "copied",
}: {
  getText: () => string;
  className?: string;
  label?: string;
  copiedLabel?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  async function onClick() {
    const ok = await copyToClipboard(getText());
    if (!ok) return;
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1200);
  }

  return (
    <button
      className={`copy-pill${copied ? " is-copied" : ""}${className ? ` ${className}` : ""}`}
      type="button"
      aria-label={copied ? copiedLabel : label}
      onClick={() => void onClick()}
    >
      {copied ? copiedLabel : label}
    </button>
  );
}
