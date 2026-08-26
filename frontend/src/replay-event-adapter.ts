import type {
  ProviderStreamEvent,
  ReplayRawEvent,
  ReplayTimelineEvent,
  SessionTool,
} from "./api";

type RawRecord = Record<string, unknown>;

type ProviderEventInput =
  | Pick<ProviderStreamEvent, "payload">
  | Pick<ReplayRawEvent, "raw">;

export type ReplayEventPresentation = {
  messageRole: "user" | "assistant";
  messageText: string | null;
  message: boolean;
  tool: SessionTool | null;
};

function asRecord(value: unknown): RawRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as RawRecord
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function providerEventPayload(event: ProviderEventInput): RawRecord | null {
  return "payload" in event ? asRecord(event.payload) : asRecord(event.raw.payload);
}

function rawContentText(value: unknown): string | null {
  const direct = stringValue(value);
  if (direct) return direct;
  if (!Array.isArray(value)) return null;
  for (const blockValue of value) {
    const block = asRecord(blockValue);
    if (!block || block.type !== "text") continue;
    const text = stringValue(block.text);
    if (text) return text;
  }
  return null;
}

function codexItem(payload: RawRecord | null): RawRecord | null {
  return asRecord(asRecord(payload?.params)?.item);
}

function toolInputText(value: unknown): string {
  const direct = stringValue(value);
  if (direct) return direct;
  const record = asRecord(value);
  if (!record) return "";
  try {
    return JSON.stringify(record);
  } catch {
    return "";
  }
}

function firstToolTarget(input: RawRecord | null): string {
  if (!input) return "";
  for (const key of ["command", "file_path", "filePath", "path", "pattern", "query", "url"]) {
    const value = stringValue(input[key]);
    if (value) return value;
  }
  return "";
}

function archetypeForTool(name: string, itemType = ""): string {
  const normalized = name.toLowerCase();
  if (itemType === "commandExecution" || ["bash", "shell", "terminal"].includes(normalized)) {
    return "bash";
  }
  if (["read", "cat", "notebookread"].includes(normalized)) return "read";
  if (["edit", "write", "apply_patch", "filechange"].includes(normalized)) return "edit";
  if (["grep", "glob", "search"].includes(normalized)) return "search";
  return "tool";
}

export function providerToolFromEvent(
  event: ReplayTimelineEvent,
  rawEvent: ReplayRawEvent | null,
): SessionTool | null {
  const payload = rawEvent ? providerEventPayload(rawEvent) : null;
  const item = codexItem(payload);
  if (item) {
    const itemType = stringValue(item.type) ?? "";
    const toolLike = ["commandExecution", "fileChange", "functionCall", "mcpToolCall", "webSearchCall"].includes(itemType);
    if (toolLike) {
      const name = itemType === "commandExecution"
        ? "Bash"
        : stringValue(item.name) ?? itemType;
      const input = toolInputText(item.command ?? item.arguments ?? item.input ?? firstToolTarget(item));
      const target = (stringValue(item.command) ?? firstToolTarget(item)) || input;
      const exitCode = item.exitCode;
      const ok = typeof exitCode === "number" ? exitCode === 0 : event.bookmark === "error" ? false : null;
      return {
        name,
        input,
        output: null,
        ok,
        archetype: archetypeForTool(name, itemType),
        summary: `${name.toLowerCase()}${target ? ` ${target}` : ""}`,
      };
    }
  }

  const message = asRecord(payload?.message);
  const content = message?.content;
  if (Array.isArray(content)) {
    for (const blockValue of content) {
      const block = asRecord(blockValue);
      if (!block || (block.type !== "tool_use" && block.type !== "tool_result")) continue;
      if (block.type === "tool_use") {
        const name = stringValue(block.name) ?? "tool";
        const inputRecord = asRecord(block.input);
        const target = firstToolTarget(inputRecord);
        return {
          name,
          input: toolInputText(block.input),
          output: null,
          ok: null,
          archetype: archetypeForTool(name),
          summary: `${name}${target ? ` ${target}` : ""}`,
        };
      }
      const resultText = rawContentText(block.content) ?? "";
      return {
        name: "tool result",
        input: resultText,
        output: null,
        ok: block.is_error === true ? false : null,
        archetype: "tool",
        summary: `tool result${resultText ? ` ${resultText}` : ""}`,
      };
    }
  }

  if (/tool_use|tool_result|commandExecution/i.test(event.summary)) {
    const summary = event.summary.trim() || event.kind;
    const parts = summary.split(/\s+/, 2);
    return {
      name: parts[0] ?? event.kind,
      input: "",
      output: null,
      ok: event.bookmark === "error" ? false : null,
      archetype: archetypeForTool(parts[0] ?? event.kind),
      summary,
    };
  }
  return null;
}

export function replayEventPresentation(
  event: ReplayTimelineEvent,
  rawEvent: ReplayRawEvent | null,
): ReplayEventPresentation {
  const payload = rawEvent ? providerEventPayload(rawEvent) : null;
  const message = asRecord(payload?.message);
  const messageRole = stringValue(message?.role);
  const item = codexItem(payload);
  const itemType = stringValue(item?.type);
  const isMessageKind = ["claude_user", "claude_assistant", "codex_user", "codex_assistant"].includes(event.kind);
  const isMessageItem = itemType === "userMessage" || itemType === "agentMessage";
  const tool = providerToolFromEvent(event, rawEvent);

  let messageText: string | null = null;
  if (messageRole === "user" || messageRole === "assistant") {
    messageText = rawContentText(message?.content);
  }
  if (!messageText && isMessageItem) {
    messageText = stringValue(item?.text) ?? rawContentText(item?.content) ?? rawContentText(item?.summary);
  }
  if (!messageText && isMessageKind) messageText = event.summary;
  if (!messageText && isMessageItem) messageText = event.summary;

  return {
    messageRole: itemType === "userMessage" || event.kind.includes("user") ? "user" : "assistant",
    messageText,
    message: (isMessageItem || isMessageKind) && tool === null,
    tool,
  };
}
