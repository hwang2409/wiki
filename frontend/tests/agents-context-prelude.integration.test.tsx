// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { SpawnWorkerModal } from "../src/agents";
import type { AgentModelOption } from "../src/api";

const { preview } = vi.hoisted(() => ({ preview: vi.fn() }));

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    previewAgentContextPrelude: preview,
    spawnAgentWorker: vi.fn(),
  };
});

const models: AgentModelOption[] = [
  {
    id: "codex-test",
    label: "Codex test",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: true,
    default_orchestrator: false,
  },
];

beforeEach(() => {
  preview.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("context preview is off by default", () => {
  render(
    <SpawnWorkerModal
      models={models}
      orchestrators={[]}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />,
  );
  // Context prelude lives inside Advanced (WIKI-154 finding 4); open it.
  fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));

  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  expect(
    (screen.getByRole("textbox", { name: "Context prelude" }) as HTMLTextAreaElement).disabled,
  ).toBe(true);
  expect(preview).not.toHaveBeenCalled();
});

test("spawn stays disabled while the enabled preview is loading", async () => {
  vi.useFakeTimers();
  preview.mockReturnValue(new Promise(() => undefined));
  render(
    <SpawnWorkerModal
      models={models}
      orchestrators={[]}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />,
  );

  fireEvent.change(screen.getByPlaceholderText("WIKI-4"), { target: { value: "WIKI-180" } });
  fireEvent.change(screen.getByPlaceholderText("Tell the worker exactly what to do."), {
    target: { value: "run tests" },
  });
  fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));
  fireEvent.click(screen.getByRole("checkbox"));

  await vi.advanceTimersByTimeAsync(250);
  expect(preview).toHaveBeenCalledTimes(1);
  expect(
    (screen.getByRole("button", { name: "Spawn" }) as HTMLButtonElement).disabled,
  ).toBe(true);
});
