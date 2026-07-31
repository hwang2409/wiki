import { useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

// WIKI-222: single source for "when does stream output stop flowing at full
// height". Line-based renderers (BoundedPreview) clip past STREAM_CLAMP_LINES;
// height-based renderers (StreamClamp, artifact tables) clamp past
// STREAM_CLAMP_PX. Both express the same ~one-screenful threshold.
export const STREAM_CLAMP_LINES = 40;
export const STREAM_CLAMP_PX = 640;
// Don't clamp content that is only barely past the threshold — a clamp that
// hides a few pixels reads as a rendering bug, not an affordance.
export const STREAM_CLAMP_SLACK_PX = 96;

export function useStreamHeightOverflow(
  ref: RefObject<HTMLElement | null>,
  enabled: boolean = true,
): boolean {
  const [overflowing, setOverflowing] = useState(false);
  useLayoutEffect(() => {
    if (!enabled) return;
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      setOverflowing(el.scrollHeight > STREAM_CLAMP_PX + STREAM_CLAMP_SLACK_PX);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    // While clamped the element's box size is fixed, so ResizeObserver goes
    // quiet even as streamed content keeps growing inside it. That is fine:
    // overflowing is already true and can only flip back via re-mount.
    return () => observer.disconnect();
  }, [enabled, ref]);
  return overflowing;
}

export function StreamClamp({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  const overflowing = useStreamHeightOverflow(bodyRef);

  const toggle = () => {
    // Collapsing a tall block from its bottom edge would teleport the
    // reader far past it — keep the block's top in view instead.
    if (expanded) {
      const el = wrapRef.current;
      if (el && el.getBoundingClientRect().top < 0) {
        requestAnimationFrame(() => el.scrollIntoView({ block: "start" }));
      }
    }
    setExpanded(!expanded);
  };

  const clamped = overflowing && !expanded;
  return (
    <div
      className={`stream-clamp${clamped ? " is-clamped" : ""}${className ? ` ${className}` : ""}`}
      ref={wrapRef}
    >
      <div
        className="stream-clamp-body"
        ref={bodyRef}
        style={clamped ? { maxHeight: STREAM_CLAMP_PX } : undefined}
      >
        {children}
      </div>
      {overflowing ? (
        <button className="stream-clamp-toggle" type="button" onClick={toggle}>
          {clamped ? "show all" : "collapse"}
        </button>
      ) : null}
    </div>
  );
}
