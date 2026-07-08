import type { Note, NoteDraft, NoteSummary } from "./types";

function encodeNotePath(path: string) {
  return path.split("/").map(encodeURIComponent).join("/");
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {})
    }
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = body?.detail ?? `Request failed with ${response.status}`;
    throw new Error(message);
  }

  return response.json() as Promise<T>;
}

export function listNotes() {
  return request<NoteSummary[]>("/api/notes");
}

export function searchNotes(query: string) {
  return request<NoteSummary[]>(`/api/notes?q=${encodeURIComponent(query)}`);
}

export function renameNote(path: string, newPath: string) {
  return request<{ path: string; changed: string[] }>("/api/rename", {
    method: "POST",
    body: JSON.stringify({ path, new_path: newPath })
  });
}

export function deleteNote(path: string) {
  return request<{ deleted: string; changed: string[] }>(
    `/api/notes/${encodeNotePath(path)}`,
    { method: "DELETE" }
  );
}

export type NoteLinks = {
  outgoing: string[];
  incoming: string[];
  unresolved: string[];
};

export function getLinks() {
  return request<Record<string, NoteLinks>>("/api/links");
}

export type AgentSession = {
  window: string | null;
  kind: string | null;
  role: string | null;
  model: string | null;
  session: number | null;
  spawned_at: string | null;
  ended_at?: string;
  outcome?: string;
};

export type AgentWorker = AgentSession & {
  ticket: string;
  registered: boolean;
  window_alive: boolean;
  worktree: string | null;
  log: string | null;
  orch: string | null;
  history: AgentSession[];
  state: string | null;
  pr: string | null;
  step: string | null;
  blocker: string | null;
  status_age_seconds: number | null;
};

export type ArchivedWorker = {
  ticket: string;
  archived_at: string;
  kind: string | null;
  role: string | null;
  model: string | null;
  outcome: string | null;
  state: string | null;
  pr: string | null;
  step: string | null;
};

export type Orchestrator = {
  id: string;
  window: string | null;
  window_alive: boolean;
  cwd: string | null;
  spawned_at: string | null;
  transcript_exists: boolean;
};

export function getAgents() {
  return request<{
    workers: AgentWorker[];
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
  }>("/api/agents");
}

export type SpawnWorkerKind = "cdx" | "cc";
export type SpawnWorkerRole = "plan" | "implement" | "review";
export type SpawnWorkerEffort = "minimal" | "low" | "medium" | "high" | "xhigh";

export type SpawnWorkerInput = {
  ticket: string;
  kind: SpawnWorkerKind;
  role: SpawnWorkerRole;
  model: string;
  effort: SpawnWorkerEffort | null;
  workdir: string;
  orch: string | null;
  prompt: string;
};

export type SpawnWorkerResult = {
  window: string;
  log: string;
  prompt_path: string;
};

export function spawnAgentWorker(body: SpawnWorkerInput) {
  return request<SpawnWorkerResult>("/api/agents/spawn", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type SpawnOrchestratorModel = "opus" | "sonnet";

export type SpawnOrchestratorInput = {
  id: string;
  workdir: string;
  model: SpawnOrchestratorModel;
  goal: string;
};

export type SpawnOrchestratorResult = {
  window: string;
  log: string;
  prompt_path: string;
  note: string;
};

export function spawnAgentOrchestrator(body: SpawnOrchestratorInput) {
  return request<SpawnOrchestratorResult>("/api/agents/spawn-orchestrator", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type SessionTool = {
  name: string;
  input: string;
  output: string | null;
  ok: boolean | null;
  archetype: string;
  summary: string;
  agent_id?: string;
};

export type SessionBash = {
  input: string;
  stdout: string;
  stderr: string;
};

export type SessionTask = {
  id: string;
  subject: string;
  status: "pending" | "in_progress" | "completed" | string;
  blockedBy?: string[];
  activeForm?: string;
};

export type SessionPr = { number: number; url: string };

export type SessionEvent = {
  kind:
    | "user"
    | "assistant"
    | "thinking"
    | "tool"
    | "terminal"
    | "notification"
    | "command"
    | "bash"
    | "image"
    | "tasks"
    | "interrupt"
    | "pr"
    | "marker";
  ts: string | null;
  text: string;
  tool?: SessionTool;
  bash?: SessionBash;
  tasks?: SessionTask[];
  pr?: SessionPr;
  marker?: string;
};

export type SubagentInfo = {
  id: string;
  active: boolean;
  head: string;
};

export type AgentSessionData = {
  format: "codex" | "claude";
  path: string;
  tokens: number | null;
  tasks?: SessionTask[];
  pr?: SessionPr | null;
  from: number;
  total: number;
  events: SessionEvent[];
  subagents?: SubagentInfo[];
  working?: boolean;
};

export type QueuedMessage = { text: string; queued_at: string };

export type AgentPrCheck = {
  name: string;
  state: "pass" | "fail" | "pending";
  rawState: string;
  workflow: string | null;
  detailsUrl: string | null;
};

export type AgentPrThread = {
  path: string;
  latestComment: string;
  author: string | null;
  updatedAt: string | null;
  url: string | null;
};

export type AgentPrData = {
  url: string;
  repo: string;
  title: string;
  state: string | null;
  mergeable: string | null;
  mergeStateStatus: string | null;
  reviewDecision: string | null;
  headRefName: string | null;
  additions: number;
  deletions: number;
  changedFiles: number;
  statusChecks: AgentPrCheck[];
  unresolvedThreads: AgentPrThread[];
  diff: string;
};

export function sendAgentMessage(ticket: string, text: string, mode: "now" | "on-idle") {
  return request<{ status: string }>(`/api/agents/${encodeURIComponent(ticket)}/message`, {
    method: "POST",
    body: JSON.stringify({ text, mode }),
  });
}

export function getAgentQueue(ticket: string) {
  return request<{ messages: QueuedMessage[] }>(
    `/api/agents/${encodeURIComponent(ticket)}/queue`
  );
}

export function cancelQueuedMessage(ticket: string, index: number) {
  return request<{ messages: QueuedMessage[] }>(
    `/api/agents/${encodeURIComponent(ticket)}/queue/${index}`,
    { method: "DELETE" }
  );
}

export function getAgentSession(ticket: string, after = 0) {
  return request<AgentSessionData>(
    `/api/agents/${encodeURIComponent(ticket)}/session?after=${after}`
  );
}

export type SkillInfo = { name: string; description: string };

export function getSkills() {
  return request<{ skills: SkillInfo[] }>("/api/skills");
}

export function uploadImage(mediaType: string, base64: string) {
  return request<{ path: string; url: string }>("/api/upload", {
    method: "POST",
    body: JSON.stringify({ media_type: mediaType, data: base64 }),
  });
}

export function getSubagentSession(ticket: string, agentId: string, after = 0) {
  return request<AgentSessionData>(
    `/api/agents/${encodeURIComponent(ticket)}/subagents/${encodeURIComponent(agentId)}/session?after=${after}`
  );
}

export function getAgentPr(ticket: string) {
  return request<AgentPrData>(`/api/agents/${encodeURIComponent(ticket)}/pr`);
}

export function approveAgentPr(ticket: string) {
  return request<{ status: string }>(`/api/agents/${encodeURIComponent(ticket)}/pr/approve`, {
    method: "POST",
  });
}

export type ActivityFile = {
  path: string;
  status: string;
};

export type ActivityCommit = {
  sha: string;
  date: string;
  message: string;
  files: ActivityFile[];
};

export function getActivity(limit = 50) {
  return request<ActivityCommit[]>(`/api/activity?limit=${limit}`);
}

export function getActivityDiff(sha: string) {
  return request<{ patch: string }>(`/api/activity/${sha}`);
}

export function getNote(path: string) {
  return request<Note>(`/api/notes/${encodeNotePath(path)}`);
}

export function createNote(draft: NoteDraft) {
  return request<Note>("/api/notes", {
    method: "POST",
    body: JSON.stringify({
      title: draft.title,
      path: draft.path || null,
      content: draft.content
    })
  });
}

export function updateNote(path: string, content: string) {
  return request<Note>(`/api/notes/${encodeNotePath(path)}`, {
    method: "PUT",
    body: JSON.stringify({ content })
  });
}

export type TokenSeries = {
  input: number;
  cached: number;
  output: number;
  reasoning: number;
};

export type TokenBucket = {
  ts: string;
  series: Record<string, TokenSeries>;
};

export type TokensResponse = {
  buckets: TokenBucket[];
  totals: TokenSeries;
  models: string[];
  clis: string[];
  sessions_scanned: number;
  bucket: "hour" | "day";
};

export type TokensQuery = {
  from?: string;
  to?: string;
  bucket?: "hour" | "day";
  cli?: string;
  model?: string;
};

export function getTokens(params: TokensQuery = {}) {
  const search = new URLSearchParams();
  if (params.from) search.set("from", params.from);
  if (params.to) search.set("to", params.to);
  if (params.bucket) search.set("bucket", params.bucket);
  if (params.cli) search.set("cli", params.cli);
  if (params.model) search.set("model", params.model);
  const qs = search.toString();
  return request<TokensResponse>(`/api/tokens${qs ? `?${qs}` : ""}`);
}
