import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) =>
      !element.hasAttribute("aria-hidden") &&
      (element.offsetParent !== null || element.getClientRects().length > 0),
  );
}

export function useModalA11y<T extends HTMLElement>(open: boolean) {
  const dialogRef = useRef<T | null>(null);
  const invokerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const active = document.activeElement;
    invokerRef.current = active instanceof HTMLElement ? active : null;
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

    const focusFirst = () => {
      const first = focusableWithin(dialog)[0];
      (first ?? dialog).focus();
    };
    const frame = window.requestAnimationFrame(focusFirst);
    const onKeyDown = (event: KeyboardEvent) => {
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
      } else if (activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);

    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown, true);
      for (const root of inertRoots) {
        if (root.inert === null) root.element.removeAttribute("inert");
        else root.element.setAttribute("inert", root.inert);
        if (root.ariaHidden === null) root.element.removeAttribute("aria-hidden");
        else root.element.setAttribute("aria-hidden", root.ariaHidden);
      }
      const invoker = invokerRef.current;
      if (invoker && document.contains(invoker)) invoker.focus();
    };
  }, [open]);

  return dialogRef;
}
