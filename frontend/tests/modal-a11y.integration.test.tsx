// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { useRef, useState } from "react";
import { useModalA11y, type FocusReturnRef } from "../src/modal-a11y";

function KeyboardInvokerHarness() {
  const [open, setOpen] = useState(false);
  const fallbackRef = useRef<FocusReturnRef>({ current: null });
  const dialogRef = useModalA11y<HTMLDivElement>(open, () => setOpen(false), fallbackRef.current);

  return (
    <>
      <button ref={(element) => { fallbackRef.current.current = element; }} type="button">
        fallback opener
      </button>
      <button type="button">pointer target</button>
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
  const [stalePointerTarget, setStalePointerTarget] = useState(false);
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
      {menuOpen || stalePointerTarget ? (
        <button
          type="button"
          onClick={() => {
            setMenuOpen(false);
            setStalePointerTarget(true);
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

function DisconnectedFallbackHarness() {
  const [showFallback, setShowFallback] = useState(true);
  const [open, setOpen] = useState(false);
  const fallbackRef = useRef<FocusReturnRef>({ current: null });
  const dialogRef = useModalA11y<HTMLDivElement>(open, () => setOpen(false), fallbackRef.current);

  return (
    <>
      {showFallback ? (
        <button ref={(element) => { if (element) fallbackRef.current.current = element; }} type="button">
          disconnected fallback
        </button>
      ) : null}
      <button type="button">live interaction target</button>
      <button
        type="button"
        onClick={() => {
          setShowFallback(false);
          setOpen(true);
        }}
      >
        open disconnected fallback
      </button>
      {open ? (
        <div ref={dialogRef} role="dialog" tabIndex={-1}>
          dialog
        </div>
      ) : null}
    </>
  );
}

function DialogDescendantHarness() {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(true);
  const fallbackRef = useRef<FocusReturnRef>({ current: null });
  const dialogRef = useModalA11y<HTMLDivElement>(
    open,
    () => {
      setOpen(false);
      setMounted(false);
    },
    fallbackRef.current,
  );

  return (
    <>
      <button type="button">live descendant candidate target</button>
      <button type="button" onClick={() => setOpen(true)}>
        open descendant candidate
      </button>
      {mounted ? (
        <div ref={dialogRef} hidden={!open} role="dialog" tabIndex={-1}>
          <button ref={(element) => { fallbackRef.current.current = element; }} type="button">
            dialog descendant candidate
          </button>
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
  test("prefers a live fallback over a live pointer target after keyboard open", async () => {
    render(<KeyboardInvokerHarness />);
    const fallback = screen.getByRole("button", { name: "fallback opener" });
    const pointerTarget = screen.getByRole("button", { name: "pointer target" });
    const keyboardInvoker = screen.getByRole("button", { name: "keyboard invoker" });

    fireEvent.pointerDown(pointerTarget);
    keyboardInvoker.focus();
    fireEvent.keyDown(keyboardInvoker, { key: "Enter" });
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(fallback);
  });

  test("prefers the stored context origin over a stale connected pointer target", async () => {
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

  test("falls through to the interaction target when fallback is disconnected", async () => {
    render(<DisconnectedFallbackHarness />);
    const interactionTarget = screen.getByRole("button", { name: "live interaction target" });
    const opener = screen.getByRole("button", { name: "open disconnected fallback" });

    fireEvent.pointerDown(interactionTarget);
    fireEvent.click(opener);
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(interactionTarget);
  });

  test("rejects a dialog descendant as a focus-restore candidate", async () => {
    render(<DialogDescendantHarness />);
    const interactionTarget = screen.getByRole("button", { name: "live descendant candidate target" });
    const opener = screen.getByRole("button", { name: "open descendant candidate" });

    fireEvent.pointerDown(interactionTarget);
    fireEvent.click(opener);
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(interactionTarget);
  });
});
