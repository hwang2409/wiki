import { useCallback, useEffect, useRef, type RefObject } from "react";

export type MenuCloseReason = "escape" | "tab";

type UseMenuKeyboardOptions = {
  open: boolean;
  onClose: (reason: MenuCloseReason) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
};

function getMenuItems(menu: HTMLDivElement | null): HTMLElement[] {
  return Array.from(
    menu?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [],
  );
}

function focusMenuItem(items: HTMLElement[], index: number) {
  items.forEach((item, itemIndex) => {
    item.tabIndex = itemIndex === index ? 0 : -1;
  });
  items[index]?.focus();
}

export function useMenuKeyboard({ open, onClose, triggerRef }: UseMenuKeyboardOptions) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      const items = getMenuItems(menuRef.current);
      if (items.length > 0) focusMenuItem(items, 0);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      window.setTimeout(() => onClose("tab"), 0);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onClose("escape");
      window.requestAnimationFrame(() => triggerRef.current?.focus());
      return;
    }

    const items = getMenuItems(menuRef.current);
    if (items.length === 0) return;
    const activeIndex = items.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = activeIndex < 0 ? 0 : (activeIndex + 1) % items.length;
    else if (event.key === "ArrowUp") nextIndex = activeIndex < 0 ? items.length - 1 : (activeIndex - 1 + items.length) % items.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = items.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      if (activeIndex >= 0) {
        event.preventDefault();
        items[activeIndex]?.click();
      }
      return;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    focusMenuItem(items, nextIndex);
  }, [onClose, triggerRef]);

  return { menuRef, onKeyDown };
}
