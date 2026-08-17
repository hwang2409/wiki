import { useLayoutEffect, useRef } from "react";

export type FocusReturnRef = { current: HTMLElement | null };

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

let scrollLockCount = 0;
let previousBodyOverflow: string | null = null;
let lastInteractionTarget: HTMLElement | null = null;

document.addEventListener(
  "pointerdown",
  (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    lastInteractionTarget = target.closest("button, a, [role='button']") ?? target;
  },
  true,
);

function lockDocumentScroll() {
  if (scrollLockCount === 0) {
    previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  scrollLockCount += 1;
}

function unlockDocumentScroll() {
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0 && previousBodyOverflow !== null) {
    document.body.style.overflow = previousBodyOverflow;
    previousBodyOverflow = null;
  }
}

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) =>
      !element.hasAttribute("aria-hidden") &&
      (element.offsetParent !== null || element.getClientRects().length > 0),
  );
}

export function useModalA11y<T extends HTMLElement>(
  open: boolean,
  onEscape?: () => void,
  fallbackRef?: FocusReturnRef,
  initialFocusRef?: FocusReturnRef,
) {
  const dialogRef = useRef<T | null>(null);
  const invokerRef = useRef<HTMLElement | null>(null);
  const onEscapeRef = useRef(onEscape);
  onEscapeRef.current = onEscape;

  useLayoutEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    lockDocumentScroll();
    const active = document.activeElement;
    const fallback = fallbackRef?.current;
    const interactionTarget = lastInteractionTarget && document.contains(lastInteractionTarget)
      ? lastInteractionTarget
      : null;
    lastInteractionTarget = null;
    invokerRef.current = interactionTarget ?? (active instanceof HTMLElement ? active : null) ?? fallback ?? null;
    const inertRoots: Array<{
      element: HTMLElement;
      inert: string | null;
      ariaHidden: string | null;
    }> = [];

    const marked = new Set<HTMLElement>();
    const markInert = (element: HTMLElement) => {
      if (marked.has(element) || element === dialog || element.hasAttribute("aria-hidden")) return;
      marked.add(element);
      inertRoots.push({
        element,
        inert: element.getAttribute("inert"),
        ariaHidden: element.getAttribute("aria-hidden"),
      });
      element.setAttribute("inert", "");
      element.setAttribute("aria-hidden", "true");
    };

    let node: HTMLElement = dialog;
    while (node.parentElement && node.parentElement !== document.body) {
      const parent = node.parentElement;
      for (const sibling of Array.from(parent.children)) {
        if (sibling !== node && sibling instanceof HTMLElement) markInert(sibling);
      }
      node = parent;
    }
    for (const child of Array.from(document.body.children)) {
      if (child.tagName === "SCRIPT" || child.tagName === "STYLE" || child.contains(dialog)) continue;
      if (child instanceof HTMLElement) markInert(child);
    }

    const initialFocus = initialFocusRef?.current ?? focusableWithin(dialog)[0] ?? dialog;
    initialFocus.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        const onEscape = onEscapeRef.current;
        if (!onEscape) return;
        event.preventDefault();
        event.stopPropagation();
        onEscape();
        return;
      }
      if (event.key !== "Tab") return;
      const focusables = focusableWithin(dialog);
      if (focusables.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables.at(-1)!;
      const activeElement = document.activeElement;
      if (event.shiftKey) {
        if (activeElement === first || !dialog.contains(activeElement)) {
          event.preventDefault();
          last.focus();
        }
      } else if (activeElement === last || !dialog.contains(activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      unlockDocumentScroll();
      for (const root of inertRoots) {
        if (root.inert === null) root.element.removeAttribute("inert");
        else root.element.setAttribute("inert", root.inert);
        if (root.ariaHidden === null) root.element.removeAttribute("aria-hidden");
        else root.element.setAttribute("aria-hidden", root.ariaHidden);
      }
      window.requestAnimationFrame(() => {
        const invoker = invokerRef.current;
        const fallback = fallbackRef?.current;
        const target = invoker && document.contains(invoker)
          ? invoker
          : fallback && document.contains(fallback)
            ? fallback
            : null;
        target?.focus();
      });
    };
  }, [fallbackRef, initialFocusRef, open]);

  return dialogRef;
}
