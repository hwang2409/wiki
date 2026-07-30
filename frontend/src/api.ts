import type { Note, NoteDraft, NoteSummary } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message ?? `Request failed with ${response.status}`;
    throw new ApiError(message, response.status);
  }

  return response.json() as Promise<T>;
}

export function listNotes() {
  return request<NoteSummary[]>("/api/notes");
}

export function searchNotes(query: string) {
  return request<NoteSummary[]>(`/api/notes?q=${encodeURIComponent(query)}`);
}

export type PaletteResultKind = "session" | "ticket" | "artifact" | "note";

export type PaletteResult = {
  kind: PaletteResultKind;
  id: string;
  title: string;
  subtitle: string;
  url: string;
  updated_at: string | null;
  score: number;
  artifact_id?: string;
  ticket?: string;
};

export function searchPalette(query: string, limit = 30, signal?: AbortSignal) {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return request<{ results: PaletteResult[] }>(`/api/palette/search?${params}`, {
    signal,
  });
}

export type FileSummary = {
  path: string;
  size: number;
  updated_at: string;
};

export type FileTree = {
  files: FileSummary[];
  truncated: boolean;
};

export type Workspace = {
  id: string;
  root: string;
  live: boolean;
};

export type FileContent = {
  path: string;
  size: number;
  content: string | null;
  binary: boolean;
  error: string | null;
};

export function listWorkspaces() {
  return request<{ workspaces: Workspace[] }>("/api/workspaces");
}

export function listFiles(workspace = "wiki") {
  return request<FileTree>(`/api/files/tree?workspace=${encodeURIComponent(workspace)}`);
}

export function fileContentRequestPath(workspace: string, path: string) {
  return `/api/files/content?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`;
}

export function getFileContent(workspace: string, path: string) {
  return request<FileContent>(fileContentRequestPath(workspace, path));
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
  run_id?: string | null;
  runtime_state?: string | null;
  state_reason?: string | null;
  control_attached?: boolean;
  provider_session_id?: string | null;
  provider_pid?: number | null;
  kind: string | null;
  role: string | null;
  model: string | null;
  effort?: SpawnWorkerEffort | null;
  desired_model?: string | null;
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
  latest_event_at: string | null;
  latest_event_seq: number | null;
  last_viewed_at: string | null;
  last_viewed_seq: number | null;
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
  run_id?: string | null;
  runtime_state?: string | null;
  state_reason?: string | null;
  control_attached?: boolean;
  provider_session_id?: string | null;
  provider_pid?: number | null;
  cwd: string | null;
  kind: SpawnWorkerKind | null;
  model: string | null;
  effort: SpawnWorkerEffort | null;
  spawned_at: string | null;
  transcript_exists: boolean;
  log?: string | null;
};

export function getAgents() {
  return request<{
    workers: AgentWorker[];
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
  }>("/api/agents");
}

export type MarkViewedResult = {
  run_id: string;
  last_viewed_at: string;
  last_viewed_seq: number;
  latest_event_at: string;
  latest_event_seq: number;
};

export function markRunViewed(runId: string, seq: number | null) {
  return request<MarkViewedResult>(
    `/api/agents/runs/${encodeURIComponent(runId)}/viewed`,
    {
      method: "POST",
      body: JSON.stringify(seq === null ? {} : { seq }),
    }
  );
}

export type DashboardTicket = {
  ticket: string;
  description: string;
  pr: string | null;
  repo: string | null;
  enriched: boolean;
  status: string;
  detail: string | null;
  date: string | null;
  live: boolean;
  role: string | null;
  kind: string | null;
};

export function getDashboardTickets(signal?: AbortSignal) {
  return request<{ tickets: DashboardTicket[]; repo_allowlist: string[] }>(
    "/api/dashboard/tickets",
    signal ? { signal } : undefined
  );
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
  window: string | null;
  run_id: string;
  log: string | null;
  prompt_path: string | null;
};

export function spawnAgentWorker(body: SpawnWorkerInput) {
  return request<SpawnWorkerResult>("/api/agents/spawn", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type SpawnOrchestratorInput = {
  id: string;
  workdir: string;
  kind: SpawnWorkerKind;
  model: string;
  effort: SpawnWorkerEffort | null;
  goal: string;
};

export type SpawnOrchestratorResult = {
  window: string | null;
  run_id: string;
  log: string | null;
  prompt_path: string | null;
  note: string;
};

export function spawnAgentOrchestrator(body: SpawnOrchestratorInput) {
  return request<SpawnOrchestratorResult>("/api/agents/spawn-orchestrator", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type AgentModelOption = {
  id: string;
  label: string;
  kind: SpawnWorkerKind;
  provider: "claude" | "codex";
  supports_reasoning_effort: boolean;
  default_worker: boolean;
  default_orchestrator: boolean;
};

export function getAgentModels() {
  return request<{ models: AgentModelOption[] }>("/api/models");
}

export type ReplaceAgentResult = {
  id: string;
  type: "worker" | "orchestrator";
  window: string | null;
  run_id?: string;
  log: string | null;
  prompt_path: string | null;
  model?: string;
  registration?: unknown;
};

export type ReplaceAgentInput = {
  kind?: SpawnWorkerKind;
  model?: string;
  effort?: SpawnWorkerEffort;
};

export function replaceAgent(id: string, input: ReplaceAgentInput = {}) {
  return request<ReplaceAgentResult>(`/api/agents/${encodeURIComponent(id)}/replace`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export type SetAgentModelResult = {
  status: "queued" | "applied" | "canceled" | string;
  desired_model: string | null;
};

export function setAgentModel(id: string, model: string) {
  return request<SetAgentModelResult>(`/api/agents/${encodeURIComponent(id)}/set-model`, {
    method: "POST",
    body: JSON.stringify({ model }),
  });
}

export function cancelAgentModelChange(id: string) {
  return request<SetAgentModelResult>(`/api/agents/${encodeURIComponent(id)}/set-model`, {
    method: "DELETE",
  });
}

export type AgentControlAction = "interrupt" | "resume" | "stop" | "archive";

export type AgentControlResult = {
  run_id: string;
  agent_id: string;
  state: string;
  state_reason?: string | null;
};

export function controlAgent(id: string, action: AgentControlAction) {
  return request<AgentControlResult>(
    `/api/agents/${encodeURIComponent(id)}/${action}`,
    { method: "POST" },
  );
}

export type ArchiveOutcome = "merged" | "closed" | "abandoned";

export function archiveAgent(id: string, options: { outcome?: ArchiveOutcome | null } = {}) {
  const body =
    options.outcome != null ? JSON.stringify({ outcome: options.outcome }) : undefined;
  return request<AgentControlResult>(`/api/agents/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    body,
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

export type SessionQuestion = {
  tool_use_id: string;
  prompt: string;
  header?: string | null;
  options: string[];
  multi_select: boolean;
  answered_option: number | null;
  answered_options: number[];
  custom_reply: string | null;
};

export type SessionDisposition = "rendered" | "summarized" | "intentionally_ignored" | "unknown";

export type SessionDispositionCounts = {
  rendered: number;
  summarized: number;
  ignored: number;
  unknown: number;
};

export type SessionMeta = {
  custom_title?: string;
  agent_name?: string;
  thinking_tokens?: {
    total?: number;
    estimated_tokens?: number | null;
    estimated_tokens_delta?: number | null;
  };
  rate_limit?: SessionRateLimit;
};

export type SessionInit = {
  claude_code_version?: string | null;
  model?: string | null;
  output_style?: string | null;
  cwd?: string | null;
  mcp_servers?: unknown[];
  agents?: unknown[];
  memory_paths?: unknown[];
  fast_mode_state?: string | null;
};

export type SessionTaskNotification = {
  status?: string | null;
  summary?: string | null;
  output_file?: string | null;
  task_id?: string | null;
  tool_use_id?: string | null;
};

export type SessionApiRetry = {
  attempt?: number | null;
  max_retries?: number | null;
  error?: unknown;
  error_status?: string | null;
  retry_delay_ms?: number | null;
};

export type SessionRateLimit = {
  status?: string | null;
  rateLimitType?: string | null;
  isUsingOverage?: boolean | null;
  overageStatus?: string | null;
  overageDisabledReason?: string | null;
  resetsAt?: number | null;
};

export type ProviderStreamEvent = {
  seq: number;
  raw_seq: number;
  normalized_at: string;
  disposition: "rendered" | "summarized" | "ignored" | "unknown";
  kind: string;
  lifecycle_state: string | null;
  payload: Record<string, unknown>;
};

export type ProviderRawEvent = {
  seq: number;
  received_at?: string;
  provider?: string;
  direction?: string;
  generation?: number;
  payload: Record<string, unknown>;
};

export type ProviderPendingRequest = {
  request_id: string | number;
  request_kind: string;
  received_at: string;
  raw_seq: number;
  payload: Record<string, unknown>;
};

export type ProviderEventInspector = {
  run_id: string;
  provider: string;
  state: string;
  raw_count: number;
  normalized_count: number;
  dispositions: SessionDispositionCounts;
  pending_requests: ProviderPendingRequest[];
  current_turn_diff?: {
    turn_id: string | null;
    seq: number;
    diff: string;
  } | null;
  events: ProviderStreamEvent[];
  raw?: ProviderRawEvent[] | null;
};

export type ArtifactKind =
  | "mermaid"
  | "svg"
  | "image"
  | "table"
  | "plot"
  | "code"
  | "diff"
  | "file-list"
  | "json";

export type ArtifactFileEntry = {
  path: string;
  label?: string | null;
  size?: number | null;
  status?: string | null;
};

export type ArtifactColumn = {
  key: string;
  label: string;
  type: "string" | "number" | "date" | "link";
};

export type SessionArtifact = {
  kind: ArtifactKind;
  source?: string;
  ref?: string;
  mime?: "image/png" | "image/jpeg" | "image/webp";
  byte_size?: number;
  data_base64?: string;
  columns?: ArtifactColumn[];
  rows?: (string | number | boolean | null)[][];
  spec_vega_lite?: Record<string, unknown>;
  language?: string;
  filename?: string;
  diff_from?: string;
  files?: ArtifactFileEntry[];
  json_data?: unknown;
};

export type SessionEvent = {
  id: number;
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
    | "marker"
    | "question"
    | "artifact"
    | "claude_init"
    | "claude_task"
    | "claude_api_retry"
    | "claude_rate_limit";
  ts: string | null;
  text: string;
  disposition: SessionDisposition;
  tool?: SessionTool;
  bash?: SessionBash;
  tasks?: SessionTask[];
  pr?: SessionPr;
  marker?: string;
  encrypted?: boolean;
  question?: SessionQuestion;
  artifact_id?: string;
  title?: string | null;
  caption?: string | null;
  artifact?: SessionArtifact;
  source?: string | null;
  pending_id?: string | null;
  claude_init?: SessionInit;
  claude_task?: SessionTaskNotification;
  claude_api_retry?: SessionApiRetry;
  claude_rate_limit?: SessionRateLimit;
};

export type SubagentInfo = {
  id: string;
  active: boolean;
  head: string;
};

export type SessionPatch = {
  id: number;
  output: string | null;
  ok: boolean | null;
};

export type AgentSessionData = {
  version: 2;
  format: "codex" | "claude" | "pane-log" | "provider-events";
  path: string;
  tokens: number | null;
  model?: string | null;
  desired_model?: string | null;
  kind?: string | null;
  provider?: string | null;
  tasks?: SessionTask[];
  pr?: SessionPr | null;
  session_meta?: SessionMeta;
  dispositions?: SessionDispositionCounts;
  base: number;
  cursor: number;
  tail_from: number;
  events: SessionEvent[];
  patches: SessionPatch[];
  has_older?: boolean;
  subagents?: SubagentInfo[];
  queue?: QueuedMessage[];
  working?: boolean;
  provider_inspector?: ProviderEventInspector;
  composer_messages?: ComposerMessage[];
};

export type ComposerMessage = {
  pending_id: string;
  text: string;
  sent_at: string | null;
  echoed_at: string | null;
  seq: number;
  source?: string | null;
};

export type AgentOlderSessionData = {
  version: 2;
  format: "codex" | "claude";
  path: string;
  base: number;
  events: SessionEvent[];
  has_older: boolean;
};

export type QueuedMessage = {
  text: string;
  queued_at: string;
  pending_id?: string;
  source?: "auto" | "explicit";
};

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

export function sendAgentMessage(
  ticket: string,
  text: string,
  mode: "now" | "on-idle",
  pendingId?: string,
  dedupeKey?: string,
) {
  return request<{
    status: string;
    pending_id?: string;
    dedupe_key?: string;
    position?: number;
    messages?: QueuedMessage[];
  }>(
    `/api/agents/${encodeURIComponent(ticket)}/message`,
    {
      method: "POST",
      body: JSON.stringify({ text, mode, pending_id: pendingId, dedupe_key: dedupeKey }),
    }
  );
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

export function getAgentSession(ticket: string, after = 0, path?: string) {
  const params = new URLSearchParams({ cursor: String(after) });
  if (path) params.set("path", path);
  return request<AgentSessionData>(
    `/api/agents/${encodeURIComponent(ticket)}/session?${params.toString()}`
  );
}

export function getAgentOlderSession(ticket: string, before: number, count = 500) {
  const params = new URLSearchParams({ before: String(before), count: String(count) });
  return request<AgentOlderSessionData>(
    `/api/agents/${encodeURIComponent(ticket)}/session/older?${params.toString()}`
  );
}

export function getAgentProviderEvents(ticket: string, includeRaw = false) {
  const params = new URLSearchParams({ include_raw: String(includeRaw) });
  return request<ProviderEventInspector>(
    `/api/agents/${encodeURIComponent(ticket)}/events?${params.toString()}`,
  );
}

export function respondToAgentRequest(
  ticket: string,
  requestId: string | number,
  response: { answers: Record<string, string | string[]> } | Record<string, unknown>,
) {
  return request<AgentControlResult>(
    `/api/agents/${encodeURIComponent(ticket)}/respond`,
    {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, response }),
    },
  );
}

export type SkillInfo = { name: string; description: string };

export function getSkills() {
  return request<{ skills: SkillInfo[] }>("/api/skills");
}

export type GhPreviewKind = "pr" | "issue" | "commit";

export type GhPreviewData = {
  ok: true;
  kind: GhPreviewKind;
  title: string;
  state: string | null;
  extra: {
    mergeStateStatus?: string | null;
    checks?: {
      pass: number;
      fail: number;
      pending: number;
    };
    changedFiles?: number | null;
    updatedAt?: string | null;
    sha?: string | null;
    author?: string | null;
    date?: string | null;
  };
};

export type GhPreviewFetchResult =
  | { status: 200; etag: string | null; data: GhPreviewData }
  | { status: 304; etag: string | null };

export async function getGhPreview(url: string, etag?: string): Promise<GhPreviewFetchResult> {
  const response = await fetch(`/api/gh/preview?url=${encodeURIComponent(url)}`, {
    headers: etag ? { "If-None-Match": etag } : undefined,
  });

  if (response.status === 304) {
    return { status: 304, etag: response.headers.get("ETag") };
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = body?.error ?? body?.detail ?? `Request failed with ${response.status}`;
    throw new Error(message);
  }

  return {
    status: 200,
    etag: response.headers.get("ETag"),
    data: (await response.json()) as GhPreviewData,
  };
}

export function uploadImage(mediaType: string, base64: string) {
  return request<{ path: string; url: string }>("/api/upload", {
    method: "POST",
    body: JSON.stringify({ media_type: mediaType, data: base64 }),
  });
}

export function getSubagentSession(ticket: string, agentId: string, after = 0, path?: string) {
  const params = new URLSearchParams({ cursor: String(after) });
  if (path) params.set("path", path);
  return request<AgentSessionData>(
    `/api/agents/${encodeURIComponent(ticket)}/subagents/${encodeURIComponent(agentId)}/session?${params.toString()}`
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

export type WorkgraphNode = {
  id: string;
  kind: string;
  label: string;
  state?: string;
  worker_id?: string;
  sha?: string;
};

export type WorkgraphFinding = {
  id: string;
  severity: "BLOCKING" | "HIGH" | "MEDIUM" | "LOW" | "INFO";
  title: string;
  file?: string;
  line?: number;
  observed?: string;
  why_wrong?: string;
  do_instead?: string;
  source_worker?: string;
  source_sha?: string;
  resolved_by?: string | null;
  created_at?: string;
};

export type WorkgraphEdge = {
  kind: string;
  from: string;
  to: string;
  payload?: Record<string, unknown> & { findings?: WorkgraphFinding[]; state?: string };
  created_at: string;
  active?: boolean;
};

export type WorkgraphHealth = {
  state: string;
  open_findings: number;
  blocking: number;
  slowest_node_stall_seconds: number;
  iteration_count: number;
};

export type Workgraph = {
  ticket: string;
  orch: string;
  template?: string | null;
  created_at: string;
  updated_at: string;
  nodes: WorkgraphNode[];
  edges: WorkgraphEdge[];
  composite_health: WorkgraphHealth;
};

export type LoopStateDanger = "normal" | "warning" | "danger";

export type LoopStateHistoryEntry = {
  round: number;
  reviewer: string | null;
  spawned_at: string | null;
  verdict_state: string | null;
  verdict_at: string | null;
  routed_at: string | null;
  archived_at: string | null;
  top_finding: string | null;
  finding_signature: string | null;
};

export type LoopStateLatestVerdict = {
  state: string | null;
  reviewer: string | null;
  created_at: string | null;
  routed_at: string | null;
  top_finding: {
    id: string | null;
    title: string | null;
    severity: string | null;
  } | null;
  signature: string | null;
  findings_count: number;
};

export type LoopState = {
  round: number;
  cap: number;
  danger: LoopStateDanger;
  unrouted_verdict_count: number;
  plateau_length: number;
  latest_verdict: LoopStateLatestVerdict | null;
  latest_verdict_finding: string | null;
  history: LoopStateHistoryEntry[];
};

export type AgentWorkgraphData = {
  ok: boolean;
  source: "live" | "snapshot";
  workgraph: Workgraph;
  revision?: number;
  loop_state?: LoopState;
};

export function getAgentWorkgraph(ticket: string) {
  return request<AgentWorkgraphData>(`/api/agents/${encodeURIComponent(ticket)}/workgraph`);
}

export type FleetGraphTicket = {
  ticket: string;
  state: string;
  role: string;
  kind: string;
  edges: WorkgraphEdge[];
};

export type FleetGraphGroup = {
  orch: string;
  tickets: FleetGraphTicket[];
};

export type FleetGraphData = {
  groups: FleetGraphGroup[];
  updated_at_ns: number;
};

export function getFleetGraph(limit = 10) {
  return request<FleetGraphData>(`/api/fleet/graph?limit=${limit}`);
}

export type WorkgraphRevision = {
  revision: number;
  created_at_ns: number;
  edge_count: number;
};

export function getAgentWorkgraphRevisions(ticket: string) {
  return request<WorkgraphRevision[]>(
    `/api/agents/${encodeURIComponent(ticket)}/workgraph/revisions`
  );
}

export function getAgentWorkgraphRevision(ticket: string, revision: number, signal?: AbortSignal) {
  return request<AgentWorkgraphData>(
    `/api/agents/${encodeURIComponent(ticket)}/workgraph?revision=${revision}`,
    { signal }
  );
}

export type ReplayBookmarkKind = "steer" | "verdict" | "error";

export type ReplayTimelineEvent = {
  seq: number;
  raw_seq: number;
  ts: string | null;
  kind: string;
  disposition: string;
  lifecycle_state: string | null;
  summary: string;
  bookmark: ReplayBookmarkKind | null;
};

export type ReplayBookmark = {
  seq: number;
  kind: ReplayBookmarkKind;
  ts: string | null;
  summary: string;
  event_kind: string;
};

export type ReplayRunSummary = {
  run_id: string;
  agent_id: string | null;
  orch_id: string | null;
  role: string | null;
  provider: string | null;
  model: string | null;
  outcome: string | null;
  state: string | null;
  created_at: string | null;
  updated_at: string | null;
  total_events: number;
  initial_prompt_excerpt: string | null;
};

export type ReplayTimeline = {
  run: ReplayRunSummary;
  events: ReplayTimelineEvent[];
  next_after_seq: number | null;
  bookmarks: ReplayBookmark[];
};

export type ReplayRawEvent = {
  run_id: string;
  seq: number;
  raw: Record<string, unknown>;
};

export function getAgentReplayRuns(ticket: string, signal?: AbortSignal) {
  return request<{ ticket: string; runs: ReplayRunSummary[] }>(
    `/api/agents/${encodeURIComponent(ticket)}/replay/runs`,
    { signal }
  );
}

export function getReplayTimeline(
  runId: string,
  { afterSeq = 0, limit = 500, signal }: { afterSeq?: number; limit?: number; signal?: AbortSignal } = {},
) {
  const params = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) });
  return request<ReplayTimeline>(
    `/api/agent-runs/${encodeURIComponent(runId)}/replay/timeline?${params.toString()}`,
    { signal }
  );
}

export function getReplayRawEvent(runId: string, seq: number, signal?: AbortSignal) {
  return request<ReplayRawEvent>(
    `/api/agent-runs/${encodeURIComponent(runId)}/replay/events/${seq}`,
    { signal }
  );
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
  refreshing: boolean;
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
