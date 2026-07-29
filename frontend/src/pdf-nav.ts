// Pure helpers for PDF navigation and zoom. Extracted for unit testing.

export const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] as const;

export type ZoomMode = "fit-width" | "fit-page" | number;

export function resolveZoom(
  mode: ZoomMode,
  viewport: { width: number; height: number },
  page: { width: number; height: number },
): number {
  if (mode === "fit-width") return Math.max(0.1, (viewport.width - 48) / page.width);
  if (mode === "fit-page") {
    return Math.max(
      0.1,
      Math.min((viewport.width - 48) / page.width, (viewport.height - 48) / page.height),
    );
  }
  return mode;
}

export function snapToZoomStep(current: number, direction: 1 | -1): number {
  const steps = ZOOM_STEPS as readonly number[];
  if (direction === 1) {
    const found = steps.find((step) => step > current + 0.0001);
    return found ?? steps[steps.length - 1];
  }
  const found = [...steps].reverse().find((step) => step < current - 0.0001);
  return found ?? steps[0];
}

export type KeyNavIntent =
  | { kind: "prev" }
  | { kind: "next" }
  | { kind: "first" }
  | { kind: "last" }
  | { kind: "open-find" };

export type KeyNavInput = {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  targetIsEditable?: boolean;
};

// Bare Arrow = prev/next single step; cmd/ctrl+Arrow = jump to first/last.
// cmd/ctrl+f opens the find overlay. Editable-target inputs are ignored so
// typing in the find field doesn't page-flip the viewport.
export function resolveKeyNav(input: KeyNavInput): KeyNavIntent | null {
  if (input.targetIsEditable) return null;
  const command = Boolean(input.metaKey || input.ctrlKey);
  if (input.altKey || input.shiftKey) return null;
  if (command && input.key.toLowerCase() === "f") return { kind: "open-find" };
  if (input.key === "ArrowLeft") return command ? { kind: "first" } : { kind: "prev" };
  if (input.key === "ArrowRight") return command ? { kind: "last" } : { kind: "next" };
  if (input.key === "Home" && command) return { kind: "first" };
  if (input.key === "End" && command) return { kind: "last" };
  return null;
}

export function applyKeyNav(
  intent: KeyNavIntent,
  page: number,
  numPages: number,
): number {
  switch (intent.kind) {
    case "prev": return Math.max(1, page - 1);
    case "next": return Math.min(numPages || page, page + 1);
    case "first": return 1;
    case "last": return Math.max(1, numPages || page);
    case "open-find": return page;
  }
}

// Focal-point-preserving zoom: given the viewport's current center (in page
// coordinate space), and the current+next zoom levels, return the scroll
// offsets that keep the same page point under the center after re-layout.
export type FocalPreservationInput = {
  viewportWidth: number;
  viewportHeight: number;
  scrollLeft: number;
  scrollTop: number;
  currentZoom: number;
  nextZoom: number;
};

export function focalPreservedScroll(input: FocalPreservationInput): {
  scrollLeft: number;
  scrollTop: number;
} {
  const centerX = (input.scrollLeft + input.viewportWidth / 2) / input.currentZoom;
  const centerY = (input.scrollTop + input.viewportHeight / 2) / input.currentZoom;
  return {
    scrollLeft: Math.max(0, centerX * input.nextZoom - input.viewportWidth / 2),
    scrollTop: Math.max(0, centerY * input.nextZoom - input.viewportHeight / 2),
  };
}
