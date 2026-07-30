// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  ScreencastProvider,
  ScreencastStrip,
  SCREENCAST_VISIBLE_LINES,
} from "../src/screencast-strip";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8"
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// --------------------------------------------------------- fetch helpers

type Deferred = {
  resolve: (r: Response) => void;
  promise: Promise<Response>;
  url: string;
  headers: Record<string, string>;
  signal: AbortSignal | undefined;
};

function stubFetchQueue(): {
  calls: Deferred[];
  push: (body: unknown, opts?: { status?: number; etag?: string }) => Response;
} {
  const calls: Deferred[] = [];
  vi.stubGlobal(
    "fetch",
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      const headers: Record<string, string> = {};
      if (init?.headers) {
        for (const [k, v] of Object.entries(init.headers as Record<string, string>)) {
          headers[k] = v;
        }
      }
      let resolve!: (r: Response) => void;
      const promise = new Promise<Response>((r) => {
        resolve = r;
      });
      calls.push({ resolve, promise, url, headers, signal: init?.signal ?? undefined });
      return promise;
    }
  );
  const push = (body: unknown, opts?: { status?: number; etag?: string }) => {
    const responseHeaders: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (opts?.etag) responseHeaders["ETag"] = opts.etag;
    return new Response(
      opts?.status === 304 ? null : JSON.stringify(body),
      { status: opts?.status ?? 200, headers: responseHeaders }
    );
  };
  return { calls, push };
}

// -------------------------------------------------- IntersectionObserver

type IoUpdate = (targets: Element[], isIntersecting: boolean) => void;

function installIntersectionObserver(): { setIntersecting: IoUpdate } {
  const callbacks: Array<{
    cb: IntersectionObserverCallback;
    observed: Set<Element>;
    instance: IntersectionObserver;
  }> = [];

  class FakeIO {
    private observed = new Set<Element>();
    constructor(private cb: IntersectionObserverCallback) {
      callbacks.push({
        cb,
        observed: this.observed,
        instance: this as unknown as IntersectionObserver,
      });
    }
    observe(target: Element) {
      this.observed.add(target);
      // Default: newly-observed nodes report intersecting so the poll can
      // start. Tests toggle later via setIntersecting.
      queueMicrotask(() => {
        this.cb(
          [
            {
              target,
              isIntersecting: true,
              intersectionRatio: 1,
              boundingClientRect: {} as DOMRectReadOnly,
              intersectionRect: {} as DOMRectReadOnly,
              rootBounds: null,
              time: 0,
            } as IntersectionObserverEntry,
          ],
          this as unknown as IntersectionObserver
        );
      });
    }
    unobserve(target: Element) {
      this.observed.delete(target);
    }
    disconnect() {
      this.observed.clear();
    }
    takeRecords() {
      return [] as IntersectionObserverEntry[];
    }
    root = null;
    rootMargin = "";
    thresholds = [] as number[];
  }
  vi.stubGlobal("IntersectionObserver", FakeIO);

  const setIntersecting: IoUpdate = (targets, isIntersecting) => {
    for (const { cb, observed, instance } of callbacks) {
      const entries: IntersectionObserverEntry[] = [];
      for (const target of targets) {
        if (!observed.has(target)) continue;
        entries.push({
          target,
          isIntersecting,
          intersectionRatio: isIntersecting ? 1 : 0,
          boundingClientRect: {} as DOMRectReadOnly,
          intersectionRect: {} as DOMRectReadOnly,
          rootBounds: null,
          time: 0,
        } as IntersectionObserverEntry);
      }
      if (entries.length > 0) cb(entries, instance);
    }
  };
  return { setIntersecting };
}

let ioHandle: { setIntersecting: IoUpdate } = { setIntersecting: () => undefined };

beforeEach(() => {
  ioHandle = installIntersectionObserver();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => "visible",
  });
});

// -------------------------------------------------- tests

describe("ScreencastStrip spec", () => {
  test("spec: 20 visible lines", () => {
    expect(SCREENCAST_VISIBLE_LINES).toBe(20);
  });

  test("fixed layout: styles.css pins the tape at the 20-line height on all axes", () => {
    // jsdom does not compute CSS calc(), and styles.css is not injected
    // into the test document. Read the source instead — this is the
    // load-bearing check: if someone removes min/max-height or the size
    // containment, the strip could reflow as frames arrive and this
    // assertion breaks.
    const ruleMatch = CSS_SOURCE.match(
      /(?:^|\})\s*\.fleet-screencast-tape\s*\{([\s\S]*?)\}/
    );
    expect(ruleMatch).not.toBeNull();
    const rule = ruleMatch![1];
    expect(rule).toContain("--fleet-screencast-lines: 20");
    expect(rule).toMatch(/\bheight:\s*calc\(var\(--fleet-screencast-lines\)/);
    expect(rule).toMatch(/\bmin-height:\s*calc\(var\(--fleet-screencast-lines\)/);
    expect(rule).toMatch(/\bmax-height:\s*calc\(var\(--fleet-screencast-lines\)/);
    expect(rule).toMatch(/\boverflow:\s*hidden/);
    expect(rule).toMatch(/\bcontain:\s*layout paint size/);
  });

  test("rendered strip has a bounded tape element with no inline size override", async () => {
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    const tape = document.querySelector<HTMLDivElement>(".fleet-screencast-tape");
    expect(tape).not.toBeNull();
    // No component-level style override should sneak in — the CSS class
    // is the single source of truth for size.
    expect(tape!.getAttribute("style")).toBeNull();
    await act(async () => {
      q.calls[0]?.resolve(q.push({ workers: [] }, { etag: 'W/"e0"' }));
    });
  });
});

describe("ScreencastProvider context re-renders (R2 finding #1)", () => {
  test("second changed response renders the new frames", async () => {
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThan(0));

    // First response.
    await act(async () => {
      q.calls[0]!.resolve(
        q.push(
          {
            workers: [
              {
                ticket: "WIKI-1",
                run_id: "abc",
                frames: [{ kind: "assistant", text: "first-text", ts: null }],
              },
            ],
          },
          { etag: 'W/"1"' }
        )
      );
    });
    await waitFor(() => expect(screen.getByText("first-text")).toBeTruthy());

    // Second response with changed frames — MUST re-render the strip.
    // (Bug the reviewer flagged: memo on [loading] dropped the update.)
    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 4000,
    });
    await act(async () => {
      q.calls[1]!.resolve(
        q.push(
          {
            workers: [
              {
                ticket: "WIKI-1",
                run_id: "abc",
                frames: [
                  { kind: "assistant", text: "first-text", ts: null },
                  { kind: "assistant", text: "second-text", ts: null },
                ],
              },
            ],
          },
          { etag: 'W/"2"' }
        )
      );
    });
    await waitFor(() => expect(screen.getByText("second-text")).toBeTruthy());
  });

  test("newly-joined worker: mounting a strip mid-run renders its frames", async () => {
    const q = stubFetchQueue();
    const { rerender } = render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThan(0));
    await act(async () => {
      q.calls[0]!.resolve(
        q.push(
          {
            workers: [
              {
                ticket: "WIKI-1",
                run_id: "abc",
                frames: [{ kind: "assistant", text: "one-text", ts: null }],
              },
            ],
          },
          { etag: 'W/"1"' }
        )
      );
    });
    await waitFor(() => expect(screen.getByText("one-text")).toBeTruthy());

    // Add a second strip — provider must poll with the union of tickets
    // and the new card must render its frames from the fresh response.
    rerender(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
        <ScreencastStrip ticket="WIKI-2" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 4000,
    });
    // The re-poll should include both tickets in the URL.
    const lastCall = q.calls[q.calls.length - 1]!;
    expect(lastCall.url).toContain("ticket=WIKI-1");
    expect(lastCall.url).toContain("ticket=WIKI-2");

    await act(async () => {
      lastCall.resolve(
        q.push(
          {
            workers: [
              {
                ticket: "WIKI-1",
                run_id: "abc",
                frames: [{ kind: "assistant", text: "one-text", ts: null }],
              },
              {
                ticket: "WIKI-2",
                run_id: "def",
                frames: [{ kind: "assistant", text: "two-text", ts: null }],
              },
            ],
          },
          { etag: 'W/"3"' }
        )
      );
    });
    await waitFor(() => expect(screen.getByText("two-text")).toBeTruthy());
  });
});

describe("ScreencastProvider polling", () => {
  test("fan-out: N mounted strips produce ONE batched request", async () => {
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
        <ScreencastStrip ticket="WIKI-2" />
        <ScreencastStrip ticket="WIKI-3" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThan(0));
    const url = q.calls[0]!.url;
    expect(q.calls.length).toBe(1);
    for (const ticket of ["WIKI-1", "WIKI-2", "WIKI-3"]) {
      expect(url).toContain(`ticket=${ticket}`);
    }
    await act(async () => {
      q.calls[0]!.resolve(q.push({ workers: [] }, { etag: 'W/"e1"' }));
    });
  });

  test("If-None-Match: sent on second poll; 304 keeps previous frames", async () => {
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(1));
    expect(q.calls[0]!.headers["If-None-Match"]).toBeUndefined();

    await act(async () => {
      q.calls[0]!.resolve(
        q.push(
          {
            workers: [
              {
                ticket: "WIKI-1",
                run_id: "abc",
                frames: [{ kind: "assistant", text: "keep-me", ts: null }],
              },
            ],
          },
          { etag: 'W/"first"' }
        )
      );
    });
    await waitFor(() => expect(screen.getByText("keep-me")).toBeTruthy());

    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 4000,
    });
    expect(q.calls[1]!.headers["If-None-Match"]).toBe('W/"first"');

    await act(async () => {
      q.calls[1]!.resolve(q.push(null, { status: 304, etag: 'W/"first"' }));
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByText("keep-me")).toBeTruthy();
  });

  test("visibility-pause: hidden tab from mount time → no poll fires", async () => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 100));
    });
    expect(q.calls.length).toBe(0);
  });

  test("off-screen pause: IntersectionObserver → not intersecting → poll stops, then resumes", async () => {
    const q = stubFetchQueue();
    render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(1));
    await act(async () => {
      q.calls[0]!.resolve(q.push({ workers: [] }, { etag: 'W/"e"' }));
    });

    // Force every observed strip off-screen. The next scheduled poll
    // MUST NOT fire until the strip comes back on-screen.
    const strip = document.querySelector<HTMLElement>(".fleet-screencast")!;
    await act(async () => {
      ioHandle.setIntersecting([strip], false);
    });
    const callsAtHide = q.calls.length;
    // Wait longer than the poll interval — no new call should appear.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 2400));
    });
    expect(q.calls.length).toBe(callsAtHide);

    // Bring the strip back on-screen; a fresh poll must fire.
    await act(async () => {
      ioHandle.setIntersecting([strip], true);
    });
    await waitFor(() => expect(q.calls.length).toBeGreaterThan(callsAtHide), {
      timeout: 4000,
    });
  });

  test("cleanup: unmount aborts the in-flight fetch (signal.aborted === true)", async () => {
    const q = stubFetchQueue();
    const { unmount } = render(
      <ScreencastProvider>
        <ScreencastStrip ticket="WIKI-1" />
      </ScreencastProvider>
    );
    await waitFor(() => expect(q.calls.length).toBeGreaterThanOrEqual(1));
    const pendingSignal = q.calls[0]!.signal;
    expect(pendingSignal).toBeDefined();
    expect(pendingSignal!.aborted).toBe(false);

    unmount();

    // The provider's effect cleanup runs controller.abort() → the fetch
    // signal is now aborted. This is the load-bearing assertion.
    expect(pendingSignal!.aborted).toBe(true);
  });
});
