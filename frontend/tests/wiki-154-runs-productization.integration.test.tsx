// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useEffect, useState } from "react";

import { AgentsView, SpawnWorkerModal } from "../src/agents";
import { isAgentRefreshEvent } from "../src/agent-events";
import { getAgents } from "../src/api";
import type { AgentModelOption, AgentWorker, ArchivedWorker, Orchestrator } from "../src/api";
import { presetWorkerModel } from "../src/role-pipeline";

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    getAgentModels: vi.fn().mockResolvedValue({ models: [] }),
    getAgents: vi.fn().mockResolvedValue({ workers: [], orchestrators: [], archived: [] }),
  };
});

const worker = {
  ticket: "WIKI-1",
  registered: true,
  window: "@42",
  window_alive: true,
  worktree: "/tmp/worktrees/wiki-1",
  log: "/tmp/logs/wiki-1.jsonl",
  orch: null,
  history: [],
  state: "working",
  pr: "https://github.com/example/repo/pull/1",
  step: "doing things",
  blocker: null,
  status_age_seconds: 5,
  latest_event_at: null,
  latest_event_seq: null,
  last_viewed_at: null,
  last_viewed_seq: null,
  run_id: "run-1234-full-id",
  runtime_state: "running",
  control_attached: true,
  kind: "cc",
  role: "implement",
  model: "opus",
  session: null,
} as unknown as AgentWorker;

const archivedEntry = {
  ticket: "WIKI-0",
  archived_at: new Date(Date.now() - 60_000).toISOString(),
  kind: "cdx",
  role: "review",
  model: "gpt-5.6-sol",
  outcome: "merged",
  state: null,
  pr: null,
  step: "done",
} as unknown as ArchivedWorker;

const data = {
  workers: [worker],
  orchestrators: [] as Orchestrator[],
  archived: [archivedEntry],
  error: null,
};

function renderView(overrides: Partial<Parameters<typeof AgentsView>[0]> = {}) {
  return render(
    <AgentsView
      data={data}
      onOpenAgent={() => undefined}
      refreshTick={0}
      openTicket={null}
      onOpenTicket={() => undefined}
      {...overrides}
    />
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ workers: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    )
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("page renders Active and History section anchors with counts", () => {
  const view = renderView();
  const active = view.getByTestId("agents-section-active");
  expect(active.textContent).toContain("Active");
  expect(active.textContent).toContain("1");
  const history = view.getByTestId("agents-section-history");
  expect(history.textContent).toContain("History");
  expect(history.textContent).toContain("1");
});

test("run id, tmux, worktree path, and log path live behind the details disclosure", async () => {
  const view = renderView();
  expect(view.queryByText(/run-1234-full-id/)).toBeNull();
  expect(view.queryByText(/@42/)).toBeNull();
  expect(view.queryByText("/tmp/logs/wiki-1.jsonl")).toBeNull();

  const toggle = view.getByRole("button", { name: /details/ });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  await waitFor(() => {
    expect(view.getByText(/run-1234-full-id · running · control attached/)).toBeTruthy();
    expect(view.getByText("@42")).toBeTruthy();
    expect(view.getByText("/tmp/worktrees/wiki-1")).toBeTruthy();
    expect(view.getByText("/tmp/logs/wiki-1.jsonl")).toBeTruthy();
  });
});

test("clicking the card body opens the session preview; buttons do not", () => {
  const onOpenTicket = vi.fn();
  const view = renderView({ onOpenTicket });
  fireEvent.click(view.getByText("doing things"));
  expect(onOpenTicket).toHaveBeenCalledWith("WIKI-1");
  onOpenTicket.mockClear();
  fireEvent.click(view.getByRole("button", { name: /details/ }));
  expect(onOpenTicket).not.toHaveBeenCalled();
});

test("account banners render persisted notices from the agents payload", () => {
  const view = renderView({
    data: {
      ...data,
      account_notices: [
        {
          type: "codex_limit_no_eligible",
          tickets: ["WIKI-9"],
          reset_at: "18:00",
          ts: "2026-07-31T00:00:00Z",
        },
      ],
    },
  });
  expect(view.getByText(/Codex usage limit reached/)).toBeTruthy();
  expect(view.getByText(/replace them with Claude workers/)).toBeTruthy();
});

test("codex_rotation_failed keeps the raw error behind the details disclosure", async () => {
  const view = renderView({
    data: {
      ...data,
      account_notices: [
        {
          type: "codex_rotation_failed",
          error: "RotationError: /Users/henry/.codex-accounts/b/auth.json: permission denied",
          ts: "2026-07-31T00:00:00Z",
        },
      ],
    },
  });
  expect(view.getByText(/rotation failed/)).toBeTruthy();
  expect(view.queryByText(/permission denied/)).toBeNull();
  fireEvent.click(view.getByRole("button", { name: /Technical details/ }));
  await waitFor(() => {
    expect(view.getByText(/permission denied/)).toBeTruthy();
  });
});

test("codex_rotation failures keep failed_reasons behind the details disclosure", async () => {
  const view = renderView({
    data: {
      ...data,
      account_notices: [
        {
          type: "codex_rotation",
          from: "a",
          to: "b",
          revived: ["WIKI-2"],
          failed: ["WIKI-3"],
          failed_reasons: { "WIKI-3": "tmux window @99 gone: server exited" },
          ts: "2026-07-31T00:00:00Z",
        },
      ],
    },
  });
  expect(view.getByText(/did not resume/)).toBeTruthy();
  expect(view.queryByText(/server exited/)).toBeNull();
  fireEvent.click(view.getByRole("button", { name: /Technical details/ }));
  await waitFor(() => {
    expect(view.getByText(/WIKI-3: tmux window @99 gone: server exited/)).toBeTruthy();
  });
});

test("codex_auth_dead_revival failures keep failed_reasons behind the details disclosure", async () => {
  const view = renderView({
    data: {
      ...data,
      account_notices: [
        {
          type: "codex_auth_dead_revival",
          revived: [],
          failed: ["WIKI-4"],
          failed_reasons: { "WIKI-4": "Traceback: OSError [Errno 24]" },
          ts: "2026-07-31T00:00:00Z",
        },
      ],
    },
  });
  expect(view.getByText(/did not restart/)).toBeTruthy();
  expect(view.queryByText(/Errno 24/)).toBeNull();
  fireEvent.click(view.getByRole("button", { name: /Technical details/ }));
  await waitFor(() => {
    expect(view.getByText(/WIKI-4: Traceback: OSError \[Errno 24\]/)).toBeTruthy();
  });
});

const models: AgentModelOption[] = [
  {
    id: "gpt-5.6-sol",
    label: "GPT 5.6 Sol",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: false,
    default_orchestrator: true,
  },
  {
    id: "gpt-5.6-luna",
    label: "GPT 5.6 Luna",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: false,
    default_orchestrator: false,
  },
  {
    id: "gpt-5.4",
    label: "GPT 5.4",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: true,
    default_orchestrator: false,
  },
];

test("role pipeline presets pick the vault-default models with API fallback", () => {
  expect(presetWorkerModel(models, "cdx", "implement")).toBe("gpt-5.6-luna");
  expect(presetWorkerModel(models, "cdx", "review")).toBe("gpt-5.6-sol");
  // preset model missing from the list -> fall back to default_worker flag
  const withoutLuna = models.filter((option) => option.id !== "gpt-5.6-luna");
  expect(presetWorkerModel(withoutLuna, "cdx", "implement")).toBe("gpt-5.4");
});

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close() {
    /* noop */
  }
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

// Mirrors App.tsx: the /api/events stream bumps refreshTick via
// isAgentRefreshEvent, and AgentsView refetches /api/agents on each tick.
function NoticeRefreshHarness() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const source = new EventSource("/api/events") as unknown as FakeEventSource;
    source.onmessage = (raw: MessageEvent) => {
      const payload = JSON.parse(raw.data as string) as { type?: string };
      if (typeof payload.type === "string" && isAgentRefreshEvent(payload.type)) {
        setTick((current) => current + 1);
      }
    };
    return () => source.close();
  }, []);
  return (
    <AgentsView
      onOpenAgent={() => undefined}
      refreshTick={tick}
      openTicket={null}
      onOpenTicket={() => undefined}
    />
  );
}

test("codex_auth_verified refreshes the agents view and clears the exhausted banner", async () => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  const exhausted = {
    type: "codex_auth_dead_exhausted",
    tickets: ["WIKI-9"],
    ts: "2026-07-31T00:00:00Z",
  };
  const base = { workers: [], orchestrators: [], archived: [] };
  vi.mocked(getAgents)
    .mockResolvedValueOnce({ ...base, account_notices: [exhausted] } as never)
    .mockResolvedValue({ ...base, account_notices: [] } as never);

  const view = render(<NoticeRefreshHarness />);
  await waitFor(() => {
    expect(view.getByText(/Sign in to Codex again/)).toBeTruthy();
  });

  act(() => {
    FakeEventSource.instances[0].emit({
      type: "codex_auth_verified",
      provider: "codex",
      credential_source: "current",
      success: true,
      ts: "2026-07-31T00:01:00Z",
    });
  });

  await waitFor(() => {
    expect(view.queryByText(/Sign in to Codex again/)).toBeNull();
  });
  expect(vi.mocked(getAgents).mock.calls.length).toBeGreaterThanOrEqual(2);
});

test("spawn dialog applies role presets and previews what will be created", () => {
  const view = render(
    <SpawnWorkerModal
      models={models}
      orchestrators={[]}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />
  );
  const selects = view.container.querySelectorAll("select");
  const roleSelect = selects[1] as HTMLSelectElement;
  const modelSelect = selects[2] as HTMLSelectElement;
  expect(modelSelect.value).toBe("gpt-5.6-luna");
  fireEvent.change(roleSelect, { target: { value: "review" } });
  expect(modelSelect.value).toBe("gpt-5.6-sol");

  const preview = view.container.querySelector(".agent-spawn-preview");
  expect(preview?.textContent).toContain("This will create");
  expect(preview?.textContent).toContain("review worker");
  expect(preview?.textContent).toContain("gpt-5.6-sol");
});
