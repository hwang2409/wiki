import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Bot, Ticket, Image as ImageIcon, FileText } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { searchPalette, type PaletteResult, type PaletteResultKind } from "./api";

const KIND_ORDER: PaletteResultKind[] = ["session", "ticket", "artifact", "note"];

const KIND_LABEL: Record<PaletteResultKind, string> = {
  session: "Sessions",
  ticket: "Tickets",
  artifact: "Artifacts",
  note: "Notes",
};

const KIND_ICON: Record<PaletteResultKind, LucideIcon> = {
  session: Bot,
  ticket: Ticket,
  artifact: ImageIcon,
  note: FileText,
};

const DEBOUNCE_MS = 80;

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export type CommandPaletteProps = {
  onClose: () => void;
  onOpen: (result: PaletteResult) => void;
};

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  const days = Math.round(seconds / 86400);
  if (days < 30) return `${days}d`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo`;
  return `${Math.round(months / 12)}y`;
}

export function CommandPalette({ onClose, onOpen }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PaletteResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const invokerRef = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    const active = document.activeElement;
    invokerRef.current = active instanceof HTMLElement ? active : null;
    // Mark every top-level sibling of the portal target inert so background
    // widgets can't grab Tab or receive pointer events while the modal is up.
    const body = document.body;
    const marked: Element[] = [];
    for (const child of Array.from(body.children)) {
      if (child.contains(dialogRef.current)) continue;
      if (child.tagName === "SCRIPT" || child.tagName === "STYLE") continue;
      marked.push(child);
      child.setAttribute("inert", "");
      child.setAttribute("aria-hidden", "true");
    }
    inputRef.current?.focus();
    return () => {
      for (const child of marked) {
        child.removeAttribute("inert");
        child.removeAttribute("aria-hidden");
      }
      const invoker = invokerRef.current;
      if (invoker && document.contains(invoker) && typeof invoker.focus === "function") {
        invoker.focus();
      }
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const payload = await searchPalette(query, 30, controller.signal);
        setResults(payload.results);
        setSelected(0);
      } catch (error) {
        if ((error as { name?: string }).name !== "AbortError") {
          setResults([]);
        }
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [query]);

  const groups = useMemo(() => {
    const byKind = new Map<PaletteResultKind, PaletteResult[]>();
    for (const result of results) {
      const bucket = byKind.get(result.kind) ?? [];
      bucket.push(result);
      byKind.set(result.kind, bucket);
    }
    return KIND_ORDER.filter((kind) => (byKind.get(kind) ?? []).length > 0).map((kind) => ({
      kind,
      items: byKind.get(kind) ?? [],
    }));
  }, [results]);

  const flatResults = useMemo(() => groups.flatMap((group) => group.items), [groups]);

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(
      `[data-command-index="${selected}"]`
    );
    node?.scrollIntoView({ block: "nearest" });
  }, [selected, flatResults.length]);

  function activate(result: PaletteResult | undefined) {
    if (!result) return;
    onOpen(result);
  }

  function moveSelection(delta: number) {
    if (flatResults.length === 0) {
      setSelected(0);
      return;
    }
    setSelected((index) => (index + delta + flatResults.length) % flatResults.length);
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    // Dialog-scope handler so arrow keys / Enter / Esc / Tab still work
    // even after focus leaves the input (e.g. after clicking a result).
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      activate(flatResults[selected]);
      return;
    }
    if (event.key === "ArrowDown" || (event.ctrlKey && event.key.toLowerCase() === "j")) {
      event.preventDefault();
      moveSelection(1);
      return;
    }
    if (event.key === "ArrowUp" || (event.ctrlKey && event.key.toLowerCase() === "k")) {
      event.preventDefault();
      moveSelection(-1);
      return;
    }
    if (event.key === "Tab") {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusables = Array.from(
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
      ).filter((element) => !element.hasAttribute("disabled") && element.tabIndex !== -1);
      if (focusables.length === 0) {
        event.preventDefault();
        inputRef.current?.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else {
        if (active === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
  }

  return createPortal(
    <div className="modal-backdrop command-palette-backdrop" onClick={onClose}>
      <div
        aria-label="Command palette"
        className="quick-switcher command-palette"
        role="dialog"
        aria-modal="true"
        ref={dialogRef}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleDialogKeyDown}
      >
        <input
          ref={inputRef}
          aria-activedescendant={
            flatResults[selected]
              ? `command-palette-result-${flatResults[selected].kind}-${flatResults[selected].id}`
              : undefined
          }
          aria-controls="command-palette-results"
          aria-label="Command palette search"
          placeholder="Search sessions, tickets, artifacts, notes…"
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div
          id="command-palette-results"
          className="quick-switcher-results command-palette-results"
          ref={listRef}
          role="listbox"
        >
          {groups.length > 0 ? (
            groups.map((group) => {
              const KindIcon = KIND_ICON[group.kind];
              return (
                <div className="quick-switcher-group command-palette-group" key={group.kind}>
                  <div className="quick-switcher-group-label">{KIND_LABEL[group.kind]}</div>
                  {group.items.map((item) => {
                    const index = flatResults.indexOf(item);
                    const active = index === selected;
                    return (
                      <button
                        aria-selected={active}
                        className={`quick-switcher-result command-palette-result${
                          active ? " is-selected" : ""
                        }`}
                        data-command-index={index}
                        id={`command-palette-result-${item.kind}-${item.id}`}
                        key={`${item.kind}-${item.id}`}
                        role="option"
                        type="button"
                        onClick={() => activate(item)}
                        onMouseEnter={() => setSelected(index)}
                      >
                        <KindIcon size={14} />
                        <span className="quick-switcher-name command-palette-name">
                          {item.title}
                        </span>
                        <span className="quick-switcher-path command-palette-subtitle">
                          {item.subtitle}
                        </span>
                        <span className="command-palette-time">
                          {relativeTime(item.updated_at)}
                        </span>
                      </button>
                    );
                  })}
                </div>
              );
            })
          ) : loading ? (
            <div className="quick-switcher-empty">Searching…</div>
          ) : (
            <div className="quick-switcher-empty">
              {query.trim() ? "No matches" : "Recent items will appear here"}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
}
