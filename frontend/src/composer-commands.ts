import {
  archiveAgent,
  replaceAgent,
  sendAgentMessage,
  spawnAgentWorker,
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
      const workdir = `.claude/worktrees/${ticket.toLowerCase()}`;
      const result = await spawnAgentWorker({
        ticket,
        kind,
        role,
        model,
        effort: null,
        workdir,
        orch: context.ticket,
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
      const outcome = values["outcome"]?.trim() || null;
      const result = await archiveAgent(target);
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
  const starts = COMMANDS.filter((command) => command.name.startsWith(query));
  if (starts.length > 0) return starts;
  return COMMANDS.filter((command) => command.name.includes(query));
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
