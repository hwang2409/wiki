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
  useEffect(() => {
    const was = wasOpen.current;
    wasOpen.current = open;
    if (open) {
      setClosing(false);
      return;
    }
    if (!was) return;
    setClosing(true);
    const timer = window.setTimeout(() => setClosing(false), COLLAPSE_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [open]);
  return (
    <div
      className={`disclosure-wrap${open ? " is-open" : ""}${className ? ` ${className}` : ""}`}
    >
      <div className="disclosure-inner">{open || closing ? children : null}</div>
    </div>
  );
}
