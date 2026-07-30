import { useEffect, useRef, useState, type ReactNode } from "react";

// Children stay mounted through the 160ms collapse transition so the
// grid-row animation has content to shrink, then unmount so hidden
// panes cost nothing (no polling, no DOM).
const COLLAPSE_EXIT_MS = 200;

export function DisclosureContent({
  open,
  className,
  children,
}: {
  open: boolean;
  className?: string;
  children: ReactNode;
}) {
  const [closing, setClosing] = useState(false);
  const wasOpen = useRef(open);
  if (open !== wasOpen.current) {
    wasOpen.current = open;
    // Render-phase update: `closing` must be true in the very first closed
    // render, or children unmount for one commit and lose their state.
    setClosing(!open);
  }
  useEffect(() => {
    if (!closing) return;
    const timer = window.setTimeout(() => setClosing(false), COLLAPSE_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [closing]);
  return (
    <div
      className={`disclosure-wrap${open ? " is-open" : ""}${className ? ` ${className}` : ""}`}
    >
      <div className="disclosure-inner">{open || closing ? children : null}</div>
    </div>
  );
}
