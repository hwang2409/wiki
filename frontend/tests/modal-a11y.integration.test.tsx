// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { useRef, useState } from "react";
import { useModalA11y } from "../src/modal-a11y";

function KeyboardInvokerHarness() {
  const [staleVisible, setStaleVisible] = useState(true);
  const [open, setOpen] = useState(false);
  const dialogRef = useModalA11y<HTMLDivElement>(open, () => setOpen(false));

  return (
    <>
      {staleVisible ? (
        <button type="button" onPointerDown={() => setStaleVisible(false)}>
          stale pointer target
        </button>
      ) : null}
      <button
        type="button"
        onKeyDown={(event) => {
          if (event.key === "Enter") setOpen(true);
        }}
      >
        keyboard invoker
      </button>
      {open ? (
        <div ref={dialogRef} role="dialog" tabIndex={-1}>
          dialog
        </div>
      ) : null}
    </>
  );
}

function ContextMenuHarness() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const originRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useModalA11y<HTMLDivElement>(dialogOpen, () => setDialogOpen(false), originRef);

  return (
    <>
      <button
        ref={originRef}
        type="button"
        onContextMenu={(event) => {
          event.preventDefault();
          setMenuOpen(true);
        }}
      >
        context origin
      </button>
      {menuOpen ? (
        <button
          type="button"
          onClick={() => {
            setMenuOpen(false);
            setDialogOpen(true);
          }}
        >
          Delete
        </button>
      ) : null}
      {dialogOpen ? (
        <div ref={dialogRef} role="dialog" tabIndex={-1}>
          dialog
        </div>
      ) : null}
    </>
  );
}

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("modal focus restoration", () => {
  test("ignores a stale pointer target when a keyboard invoker opens the dialog", async () => {
    render(<KeyboardInvokerHarness />);
    const stale = screen.getByRole("button", { name: "stale pointer target" });
    const keyboardInvoker = screen.getByRole("button", { name: "keyboard invoker" });

    fireEvent.pointerDown(stale);
    keyboardInvoker.focus();
    fireEvent.keyDown(keyboardInvoker, { key: "Enter" });
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(keyboardInvoker);
  });

  test("restores the stored context origin after the menu item unmounts", async () => {
    render(<ContextMenuHarness />);
    const origin = screen.getByRole("button", { name: "context origin" });

    fireEvent.contextMenu(origin);
    const deleteButton = await screen.findByRole("button", { name: "Delete" });
    fireEvent.pointerDown(deleteButton);
    fireEvent.click(deleteButton);
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(origin);
  });
});
