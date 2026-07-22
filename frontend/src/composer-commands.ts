import {
  archiveAgent,
  getAgents,
  replaceAgent,
  sendAgentMessage,
  spawnAgentWorker,
  type ArchiveOutcome,
  type SpawnWorkerEffort,
  type SpawnWorkerKind,
  type SpawnWorkerRole,
} from "./api";

export type CommandArgType = "text" | "long-text" | "enum" | "ticket" | "url" | "sha";

export type CommandArg = {
  name: string;
  type: CommandArgType;
  required: boolean;
  hint: string;
  placeholder?: string;
  options?: readonly string[];
  default?: string;
};

export type ComposerCommand = {
  name: string;
  description: string;
  args: CommandArg[];
  dispatch: (values: Record<string, string>, context: CommandContext) => Promise<CommandResult>;
};

export type CommandContext = {
  ticket: string;
};

export type CommandResult = {
  ok: boolean;
  summary: string;
  detail?: string | null;
};

const KINDS = ["cc", "cdx"] as const;
const ROLES = ["plan", "implement", "review"] as const;
const OUTCOMES = ["merged", "closed", "abandoned"] as const;
const EFFORTS = ["low", "medium", "high"] as const;

async function resolveOrchSessionId(orch: string): Promise<string> {
  // Server derives the dispatching orchestrator from this session id (H1).
  // Body `orch` is ignored for authorization — the caller must prove identity.
  const agents = await getAgents();
  const match = agents.orchestrators.find((entry) => entry.id === orch);
  const sessionId = match?.provider_session_id?.trim();
  if (!sessionId) {
    throw new Error(
      `no provider session id registered for orchestrator '${orch}' — is the UI attached to a live run?`
    );
  }
  return sessionId;
}

async function provisionWorktree(ticket: string, orch: string): Promise<string> {
  const sessionId = await resolveOrchSessionId(orch);
  const response = await fetch("/api/composer/provision-worktree", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Wiki-Session-Id": sessionId,
    },
    body: JSON.stringify({ ticket, orch }),
  });
  const body = (await response.json().catch(() => null)) as
    | { workdir?: string; detail?: string }
    | null;
  if (!response.ok || !body?.workdir) {
    throw new Error(body?.detail ?? `Provision failed (${response.status})`);
  }
  return body.workdir;
}

export const COMMANDS: readonly ComposerCommand[] = [
  {
    name: "steer",
    description: "Send a steer message to an agent",
    args: [
      {
        name: "agent-id",
        type: "ticket",
        required: true,
        hint: "ticket id (e.g. WIKI-148)",
        placeholder: "WIKI-148",
      },
      {
        name: "message",
        type: "long-text",
        required: true,
        hint: "steer body — sent as now-mode chat",
        placeholder: "fix the failing test, then re-verify",
      },
    ],
    async dispatch(values) {
      const target = values["agent-id"].trim();
      const text = values["message"].trim();
      const result = await sendAgentMessage(target, text, "now");
      return {
        ok: true,
        summary: `steered ${target}`,
        detail: result.status,
      };
    },
  },
  {
    name: "spawn",
    description: "Spawn a worker on a ticket",
    args: [
      {
        name: "ticket",
        type: "ticket",
        required: true,
        hint: "ticket id",
        placeholder: "WIKI-149",
      },
      {
        name: "kind",
        type: "enum",
        required: true,
        hint: "cc | cdx",
        options: KINDS,
        default: "cc",
      },
      {
        name: "role",
        type: "enum",
        required: true,
        hint: "plan | implement | review",
        options: ROLES,
        default: "implement",
      },
      {
        name: "model",
        type: "text",
        required: true,
        hint: "e.g. claude-opus-4-7, gpt-5.6-luna",
        placeholder: "claude-opus-4-7",
      },
      {
        name: "effort",
        type: "enum",
        required: false,
        hint: "cdx reasoning effort (defaults to high) — ignored for cc",
        options: EFFORTS,
      },
      {
        name: "goal",
        type: "long-text",
        required: true,
        hint: "kickoff prompt — brief the worker",
        placeholder: "Implement WIKI-149 per /tmp/wiki-prompts/WIKI-149.md",
      },
    ],
    async dispatch(values, context) {
      const ticket = values["ticket"].trim();
      const kind = values["kind"].trim() as SpawnWorkerKind;
      const role = values["role"].trim() as SpawnWorkerRole;
      const model = values["model"].trim();
      const prompt = values["goal"].trim();
      const effortRaw = (values["effort"]?.trim() || "") as SpawnWorkerEffort | "";
      const effort: SpawnWorkerEffort | null =
        kind === "cdx" ? (effortRaw || "high") : null;
      const orch = context.ticket.trim();
      if (!orch) {
        return {
          ok: false,
          summary: "cannot /spawn without orchestrator context",
          detail: "composer must be attached to an orchestrator session",
        };
      }
      const workdir = await provisionWorktree(ticket, orch);
      const result = await spawnAgentWorker({
        ticket,
        kind,
        role,
        model,
        effort,
        workdir,
        orch,
        prompt,
      });
      return {
        ok: true,
        summary: `spawned ${ticket} · ${role} · ${kind}`,
        detail: result.window ? `window ${result.window}` : `run ${result.run_id}`,
      };
    },
  },
  {
    name: "gate",
    description: "Run wiki gate against a PR",
    args: [
      {
        name: "pr",
        type: "url",
        required: true,
        hint: "PR number or URL",
        placeholder: "https://github.com/hwang2409/wiki/pull/116",
      },
      {
        name: "expect-sha",
        type: "sha",
        required: false,
        hint: "optional SHA prefix (--expect-sha)",
        placeholder: "abc1234",
      },
    ],
    async dispatch(values) {
      const pr = values["pr"].trim();
      const expectSha = values["expect-sha"]?.trim() || undefined;
      const response = await fetch("/api/composer/gate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pr, expect_sha: expectSha }),
      });
      const body = (await response.json().catch(() => null)) as
        | { verdict?: string; summary?: string; detail?: string }
        | null;
      if (!response.ok) {
        return {
          ok: false,
          summary: `gate error (${response.status})`,
          detail: body?.detail ?? null,
        };
      }
      const verdict = body?.verdict ?? "unknown";
      return {
        ok: verdict === "pass",
        summary: `gate ${verdict} · ${pr}`,
        detail: body?.summary ?? null,
      };
    },
  },
  {
    name: "archive",
    description: "Archive an agent worker",
    args: [
      {
        name: "agent-id",
        type: "ticket",
        required: true,
        hint: "agent ticket id",
        placeholder: "WIKI-148",
      },
      {
        name: "outcome",
        type: "enum",
        required: false,
        hint: "merged | closed | abandoned (metadata only)",
        options: OUTCOMES,
      },
    ],
    async dispatch(values) {
      const target = values["agent-id"].trim();
      const outcome = (values["outcome"]?.trim() || null) as ArchiveOutcome | null;
      const result = await archiveAgent(target, { outcome });
      return {
        ok: true,
        summary: outcome ? `archived ${target} (${outcome})` : `archived ${target}`,
        detail: result.state ?? null,
      };
    },
  },
  {
    name: "replace",
    description: "Replace an agent runtime",
    args: [
      {
        name: "agent-id",
        type: "ticket",
        required: true,
        hint: "agent ticket id",
        placeholder: "WIKI-148",
      },
      {
        name: "kind",
        type: "enum",
        required: false,
        hint: "cc | cdx (keep current if omitted)",
        options: KINDS,
      },
      {
        name: "model",
        type: "text",
        required: false,
        hint: "model id (keep current if omitted)",
        placeholder: "claude-opus-4-7",
      },
    ],
    async dispatch(values) {
      const target = values["agent-id"].trim();
      const body: { kind?: SpawnWorkerKind; model?: string } = {};
      const kind = values["kind"]?.trim();
      const model = values["model"]?.trim();
      if (kind) body.kind = kind as SpawnWorkerKind;
      if (model) body.model = model;
      const result = await replaceAgent(target, body);
      return {
        ok: true,
        summary: `replaced ${target}`,
        detail: result.window ? `window ${result.window}` : null,
      };
    },
  },
];

export function commandByName(name: string): ComposerCommand | undefined {
  return COMMANDS.find((command) => command.name === name);
}

export function filterCommands(partial: string): ComposerCommand[] {
  const query = partial.toLowerCase();
  if (!query) return [...COMMANDS];
  const scored: Array<{ command: ComposerCommand; score: number }> = [];
  for (const command of COMMANDS) {
    const score = fuzzyScore(command.name, query);
    if (score !== null) scored.push({ command, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.map((entry) => entry.command);
}

function fuzzyScore(name: string, query: string): number | null {
  if (query.length === 0) return 0;
  let score = 0;
  let cursor = 0;
  let lastMatch = -1;
  for (const ch of query) {
    const idx = name.indexOf(ch, cursor);
    if (idx === -1) return null;
    if (idx === 0) score += 20;
    if (idx === lastMatch + 1) score += 5;
    if (idx === cursor) score += 3;
    score += 1;
    lastMatch = idx;
    cursor = idx + 1;
  }
  if (name.startsWith(query)) score += 100;
  score -= (name.length - query.length) * 0.1;
  return score;
}

export function initialValues(command: ComposerCommand): Record<string, string> {
  const values: Record<string, string> = {};
  for (const arg of command.args) values[arg.name] = arg.default ?? "";
  return values;
}

export function missingRequired(
  command: ComposerCommand,
  values: Record<string, string>,
): CommandArg[] {
  return command.args.filter((arg) => arg.required && !(values[arg.name] ?? "").trim());
}

export function serializeCommand(
  command: ComposerCommand,
  values: Record<string, string>,
): string {
  const parts = [`\\/${command.name}`];
  for (const arg of command.args) {
    const raw = (values[arg.name] ?? "").trim();
    if (!raw) continue;
    parts.push(needsQuoting(raw) ? JSON.stringify(raw) : raw);
  }
  return parts.join(" ") + " ";
}

function needsQuoting(raw: string): boolean {
  return /[\s"\\]/.test(raw);
}
