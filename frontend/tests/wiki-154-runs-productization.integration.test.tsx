// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useEffect, useState } from "react";

import { AgentsView, SpawnOrchestratorModal, SpawnWorkerModal } from "../src/agents";
import { ReplaceAgentModal } from "../src/replace-agent-modal";
import { isAgentRefreshEvent } from "../src/agent-events";
import { getAgents, spawnAgentOrchestrator } from "../src/api";
import type { AgentModelOption, AgentWorker, ArchivedWorker, Orchestrator } from "../src/api";
import { presetWorkerModel } from "../src/role-pipeline";

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    getAgentModels: vi.fn().mockResolvedValue({ models: [] }),
    getAgents: vi.fn().mockResolvedValue({ workers: [], orchestrators: [], archived: [] }),
    spawnAgentOrchestrator: vi.fn(),
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
  runtime_state: "working",
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
  // jsdom does not implement IntersectionObserver; SessionSidebar mounts
  // SessionTab which uses it via useElementVisible.
  if (typeof (globalThis as unknown as { IntersectionObserver?: unknown }).IntersectionObserver === "undefined") {
    class FakeIntersectionObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords(): [] {
        return [];
      }
    }
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  }
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

  const toggle = view.getAllByRole("button", { name: /details/ })[0];
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  await waitFor(() => {
    expect(view.getByText(/run-1234-full-id · working · control attached/)).toBeTruthy();
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
  fireEvent.click(view.getAllByRole("button", { name: /details/ })[0]);
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

test("codex rotation with a null source renders and survives the frontend contract", () => {
  const view = renderView({
    data: {
      ...data,
      account_notices: [
        {
          type: "codex_rotation",
          from: null,
          to: "account-b",
          revived: [],
          failed: [],
          ts: "2026-07-31T00:00:00Z",
        },
      ],
    },
  });
  expect(view.getByText(/Codex account switched \(unset\) → account-b/)).toBeTruthy();
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

test("spawn dialog leads with ticket and prompt; role/model/effort live under Advanced", async () => {
  const view = render(
    <SpawnWorkerModal
      models={models}
      orchestrators={[]}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />
  );
  expect(view.getByRole("dialog", { name: "Spawn worker" })).toBeTruthy();
  // Default view: ticket + title + prompt. Role/provider/model/effort are hidden.
  expect(view.queryByLabelText("Role")).toBeNull();
  expect(view.queryByLabelText("Provider")).toBeNull();
  expect(view.queryByLabelText("Model")).toBeNull();
  expect(view.queryByLabelText("Reasoning effort")).toBeNull();
  // The Advanced summary shows the current preset so users can see defaults
  // without opening.
  const advanced = view.getByRole("button", { name: /Advanced/ });
  expect(advanced.textContent).toMatch(/implement/);
  expect(advanced.textContent).toMatch(/Codex/);

  // Open Advanced to expose the tuning selects.
  fireEvent.click(advanced);
  await waitFor(() => {
    expect(view.getByLabelText("Role")).toBeTruthy();
  });
  const roleSelect = view.getByLabelText("Role") as HTMLSelectElement;
  const modelSelect = view.getByLabelText("Model") as HTMLSelectElement;
  expect(modelSelect.value).toBe("gpt-5.6-luna");
  fireEvent.change(roleSelect, { target: { value: "review" } });
  expect(modelSelect.value).toBe("gpt-5.6-sol");

  const preview = view.container.querySelector(".agent-spawn-preview");
  expect(preview?.textContent).toContain("This will create");
  expect(preview?.textContent).toContain("review worker");
  expect(preview?.textContent).toContain("Codex");
  expect(preview?.textContent).toContain("gpt-5.6-sol");
});

test("spawn dialog hides kickoff byte count until it nears the 100KB limit", () => {
  const view = render(
    <SpawnWorkerModal
      models={models}
      orchestrators={[]}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />
  );
  const promptField = view.container.querySelector(
    "textarea.agent-spawn-textarea",
  ) as HTMLTextAreaElement;
  fireEvent.change(promptField, { target: { value: "small prompt" } });
  // Small prompt: no byte-count noise.
  expect(view.container.textContent).not.toMatch(/\bbytes\b/);
  // Just under the "nearing" threshold — still quiet.
  fireEvent.change(promptField, { target: { value: "x".repeat(70_000) } });
  expect(view.container.textContent).not.toMatch(/nearing the 100KB limit/);
  // Push past 80KB — the size hint appears.
  fireEvent.change(promptField, { target: { value: "x".repeat(85_000) } });
  expect(view.container.textContent).toMatch(/nearing the 100KB limit/);
});

test("replace dialog leads with change preview; provider/model live under Advanced", async () => {
  const view = render(
    <ReplaceAgentModal
      models={models}
      onClose={() => undefined}
      target={{ id: "WIKI-5", kind: "cdx", model: "gpt-5.4", effort: "high", role: "implement" }}
    />
  );
  expect(view.queryByLabelText("Provider")).toBeNull();
  expect(view.queryByLabelText("Model")).toBeNull();
  // Change preview always visible and uses friendly labels.
  const preview = view.container.querySelector(".agent-spawn-preview");
  expect(preview?.textContent).toContain("Codex");
  expect(preview?.textContent).not.toContain("cdx ·");

  const advanced = view.getByRole("button", { name: /Advanced/ });
  fireEvent.click(advanced);
  await waitFor(() => {
    expect(view.getByLabelText("Provider")).toBeTruthy();
    expect(view.getByLabelText("Model")).toBeTruthy();
  });
});

test("worker card default hides role, model, and technical actions; menu reveals Replace", async () => {
  const view = renderView();
  // Role/model badges no longer render on the default surface.
  expect(view.queryByText("implement")).toBeNull();
  expect(view.queryByText("opus")).toBeNull();
  // Replace button lives inside the overflow menu, not on the default row.
  expect(view.queryByRole("button", { name: /^Replace$/ })).toBeNull();
  // A working + control-attached worker gets Interrupt as its ONE primary
  // action; every other lifecycle control lives in the menu.
  expect(view.getByRole("button", { name: /^Interrupt$/ })).toBeTruthy();
  // Open the menu.
  fireEvent.click(view.getByRole("button", { name: /More actions for WIKI-1/ }));
  await waitFor(() => {
    expect(view.getByRole("menuitem", { name: /^Replace$/ })).toBeTruthy();
  });
  // Stop and Review are the destructive/secondary actions available for a
  // working attached worker with an open PR.
  expect(view.getByRole("menuitem", { name: /^Stop$/ })).toBeTruthy();
  expect(view.getByRole("menuitem", { name: /Review PR/ })).toBeTruthy();
});

test("Replace stays disabled with a reason when a worker has no live runtime", async () => {
  const legacyWorker = {
    ...worker,
    run_id: null,
    window: null,
    window_alive: false,
  } as unknown as AgentWorker;
  const view = renderView({ data: { ...data, workers: [legacyWorker] } });
  fireEvent.click(view.getByRole("button", { name: /More actions for WIKI-1/ }));
  await waitFor(() => {
    const replace = view.getByRole("menuitem", { name: /^Replace$/ }) as HTMLButtonElement;
    const stop = view.getByRole("menuitem", { name: /^Stop$/ }) as HTMLButtonElement;
    expect(replace.disabled).toBe(false);
    expect(replace.getAttribute("aria-disabled")).toBe("true");
    expect(replace.tabIndex).toBe(0);
    expect(replace.getAttribute("aria-describedby")).toBeTruthy();
    expect(replace.title).toBe("Legacy tmux runs must be migrated before Replace is available");
    expect(stop.getAttribute("aria-disabled")).toBe("true");
    expect(stop.title).toBe("Legacy tmux runs must be migrated before Stop is available");
    expect(view.getByText("Legacy tmux runs must be migrated before Replace is available")).toBeTruthy();
  });
  fireEvent.click(view.getByRole("menuitem", { name: /^Stop$/ }));
  fireEvent.click(view.getByRole("menuitem", { name: /^Replace$/ }));
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("primary action tracks runtime_state, not the manual worker state", () => {
  // A Claude worker can keep the manual status file at state=working
  // while its adapter is idle. Keying Interrupt off state would send the
  // idle adapter an interrupt for no reason — primary must key off
  // runtime_state.
  const divergent = { ...worker, state: "working", runtime_state: "idle" };
  const view = renderView({
    data: { ...data, workers: [divergent as unknown as AgentWorker] },
  });
  expect(view.queryByRole("button", { name: /^Interrupt$/ })).toBeNull();
  // Idle attached workers get no transport primary (Complete/Stop are
  // destructive-secondary), so there is no primary action button.
});

test("merge-ready card shows Review as primary and does not duplicate it in the menu", async () => {
  const mergeReady = { ...worker, state: "merge-ready", runtime_state: "idle" };
  const view = renderView({
    data: { ...data, workers: [mergeReady as unknown as AgentWorker] },
  });
  // Primary is Review (task-state driven).
  expect(view.getByRole("button", { name: /^Review$/ })).toBeTruthy();
  // Open the overflow menu — Review PR must NOT appear again.
  fireEvent.click(view.getByRole("button", { name: /More actions for WIKI-1/ }));
  await waitFor(() => {
    expect(view.getByRole("menu")).toBeTruthy();
  });
  expect(view.queryByRole("menuitem", { name: /Review PR/ })).toBeNull();
});

test("orchestrator row default hides kind/model/cwd; details disclosure reveals them", async () => {
  const orch: Orchestrator = {
    id: "wiki-lead",
    window: null,
    window_alive: false,
    run_id: "orch-run-1",
    runtime_state: "working",
    control_attached: true,
    kind: "cc",
    model: "opus",
    effort: null,
    cwd: "/tmp/projects/lead",
  } as unknown as Orchestrator;
  const workerUnderOrch = { ...worker, orch: "wiki-lead" };
  const view = renderView({
    data: {
      workers: [workerUnderOrch as AgentWorker],
      orchestrators: [orch],
      archived: [],
      error: null,
    },
  });
  expect(view.queryByRole("dialog", { name: "Spawn orchestrator" })).toBeNull();
  // Default surface must not surface raw kind/model/cwd on the orch row.
  const orchHead = view.container.querySelector(".agents-orch-head")!;
  expect(orchHead).toBeTruthy();
  expect(orchHead.textContent).not.toContain("opus");
  expect(orchHead.textContent).not.toContain("/tmp/projects/lead");
  // Replace + secondary lifecycle controls live in the overflow menu.
  expect(orchHead.querySelector("button.agent-replace-button")).toBeNull();
  // Open the orch overflow menu — Replace shows up.
  fireEvent.click(
    view.getByRole("button", { name: /More actions for wiki-lead/ }),
  );
  await waitFor(() => {
    expect(view.getByRole("menuitem", { name: /^Replace$/ })).toBeTruthy();
  });
});

test("orchestrator Replace stays disabled with a reason without a live runtime", async () => {
  const orch = {
    id: "wiki-legacy",
    window: null,
    window_alive: false,
    run_id: null,
    runtime_state: null,
    control_attached: false,
    kind: "cc",
    model: "opus",
    effort: null,
    cwd: "/tmp/projects/legacy",
  } as unknown as Orchestrator;
  const view = renderView({
    data: { workers: [], orchestrators: [orch], archived: [], error: null },
  });
  fireEvent.click(view.getByRole("button", { name: /More actions for wiki-legacy/ }));
  await waitFor(() => {
    const replace = view.getByRole("menuitem", { name: /^Replace$/ }) as HTMLButtonElement;
    const stop = view.getByRole("menuitem", { name: /^Stop$/ }) as HTMLButtonElement;
    expect(replace.disabled).toBe(false);
    expect(replace.getAttribute("aria-disabled")).toBe("true");
    expect(replace.title).toBe("Legacy tmux runs must be migrated before Replace is available");
    expect(stop.getAttribute("aria-disabled")).toBe("true");
    expect(stop.title).toBe("Legacy tmux runs must be migrated before Stop is available");
  });
  fireEvent.click(view.getByRole("menuitem", { name: /^Stop$/ }));
  fireEvent.click(view.getByRole("menuitem", { name: /^Replace$/ }));
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

// Per-archive selection for a ticket with multiple archives is descoped
// to WIKI-229 — the backend route currently wins on any live run and
// consults a ticket-only transcript-path cache before the archived_at
// hint. History rows may open the ticket's transcript view, but promising
// a specific archive here would be a false affordance. The archived_at
// identifier still rides through SidebarTarget → SessionTab →
// getAgentSession → /session?archived_at=… so WIKI-229 can switch on it
// once the route is discriminated.

test("history row is a quiet outcome/date summary with View transcript", async () => {
  const view = renderView({
    data: { ...data, workers: [], orchestrators: [], error: null },
  });
  const historyRow = view.container.querySelector(".agent-card.is-archived");
  expect(historyRow).toBeTruthy();
  // No kind/role/model badges on the default surface.
  expect(historyRow!.textContent).not.toContain("cdx");
  expect(historyRow!.textContent).not.toContain("review");
  expect(historyRow!.textContent).not.toContain("gpt-5.6-sol");
  // Outcome + View transcript ARE surfaced.
  expect(historyRow!.textContent).toContain("merged");
  expect(view.getByRole("button", { name: /View transcript/ })).toBeTruthy();
  // Details disclosure reveals the technical fields.
  fireEvent.click(view.getByRole("button", { name: /details/ }));
  await waitFor(() => {
    expect(view.getByText(/Codex \(cdx\)/)).toBeTruthy();
    expect(view.getByText("review")).toBeTruthy();
    expect(view.getByText("gpt-5.6-sol")).toBeTruthy();
  });
});

test("spawn orchestrator dialog leads with name and goal; provider/model live under Advanced", async () => {
  const view = render(
    <SpawnOrchestratorModal
      models={models}
      workspaceRoot="/workspace/active"
      onClose={() => undefined}
      onSpawn={() => undefined}
    />,
  );
  // Default view: only Name + Initial goal. Project dir / provider / model /
  // effort are hidden. Advanced summary shows current preset (friendly).
  expect(view.queryByLabelText("Provider")).toBeNull();
  expect(view.queryByLabelText("Model")).toBeNull();
  expect(view.queryByPlaceholderText("/tmp/project")).toBeNull();
  const advanced = view.getByRole("button", { name: /Advanced/ });
  expect(advanced.textContent).toMatch(/Claude/);

  // Open Advanced.
  fireEvent.click(advanced);
  await waitFor(() => {
    expect(view.getByLabelText("Provider")).toBeTruthy();
    expect(view.getByLabelText("Model")).toBeTruthy();
    expect(view.getByPlaceholderText("/tmp/project")).toBeTruthy();
  });

  // Provider options render friendly labels.
  const providerSelect = view.getByLabelText("Provider") as HTMLSelectElement;
  const providerOptions = Array.from(providerSelect.options).map((o) => o.text);
  expect(providerOptions).toEqual(expect.arrayContaining(["Claude", "Codex"]));
});

test("spawn orchestrator default flow reaches confirmation without opening Advanced", async () => {
  const onSpawn = vi.fn();
  vi.mocked(spawnAgentOrchestrator).mockResolvedValue({
    window: null,
    run_id: "run-orch-test",
    log: null,
    prompt_path: null,
    note: "spawned",
  });
  const view = render(
    <SpawnOrchestratorModal
      models={[
        ...models,
        {
          id: "claude-sonnet",
          label: "Claude Sonnet",
          kind: "cc",
          provider: "claude",
          supports_reasoning_effort: false,
          default_worker: false,
          default_orchestrator: true,
        },
      ]}
      workspaceRoot="/workspace/active"
      onClose={() => undefined}
      onSpawn={onSpawn}
    />,
  );
  fireEvent.change(view.getByPlaceholderText("wiki-dev"), { target: { value: "wiki-lead" } });
  fireEvent.change(view.getByPlaceholderText(/Optional\. Leave empty/), {
    target: { value: "start the fleet" },
  });
  fireEvent.click(view.getByRole("button", { name: /^Launch$/ }));
  await waitFor(() => {
    expect(view.getByRole("button", { name: /Confirm launch/ })).toBeTruthy();
  });
  fireEvent.click(view.getByRole("button", { name: /Confirm launch/ }));
  await waitFor(() => {
    expect(spawnAgentOrchestrator).toHaveBeenCalledWith(
      expect.objectContaining({ workdir: "/workspace/active" }),
    );
    expect(onSpawn).toHaveBeenCalled();
  });
});

test("spawn orchestrator keeps project directory visible when no workspace root is available", () => {
  const view = render(
    <SpawnOrchestratorModal
      models={models}
      onClose={() => undefined}
      onSpawn={() => undefined}
    />,
  );
  expect(view.getByPlaceholderText("/tmp/project")).toBeTruthy();
  expect(view.getByText("Project directory is required.")).toBeTruthy();
});

test("spawn orchestrator dialog hides goal byte count until it nears 20KB", () => {
  const view = render(
    <SpawnOrchestratorModal
      models={models}
      workspaceRoot="/workspace/active"
      onClose={() => undefined}
      onSpawn={() => undefined}
    />,
  );
  expect(view.getByRole("dialog", { name: "Spawn orchestrator" })).toBeTruthy();
  const goalField = view.container.querySelector(
    "textarea.agent-spawn-textarea",
  ) as HTMLTextAreaElement;
  fireEvent.change(goalField, { target: { value: "short goal" } });
  expect(view.container.textContent).not.toMatch(/\bbytes\b/);
  fireEvent.change(goalField, { target: { value: "y".repeat(12_000) } });
  expect(view.container.textContent).not.toMatch(/nearing the 20KB limit/);
  fireEvent.change(goalField, { target: { value: "y".repeat(17_000) } });
  expect(view.container.textContent).toMatch(/nearing the 20KB limit/);
});

test("worker card technical details expose provider/role/model when opened", async () => {
  const view = renderView();
  expect(view.queryByText(/Claude/)).toBeNull();
  fireEvent.click(view.getAllByRole("button", { name: /details/ })[0]);
  await waitFor(() => {
    expect(view.getByText(/Claude \(cc\)/)).toBeTruthy();
    expect(view.getByText("implement")).toBeTruthy();
    expect(view.getByText("opus")).toBeTruthy();
  });
});
