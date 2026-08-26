import type {
  ReplayTimelineEvent,
  SessionTool,
} from "./api";

type RawRecord = Record<string, unknown>;

export type PresentationBlock =
  | { type: "message"; role: "user" | "assistant"; text: string }
  | { type: "tool"; tool: SessionTool }
  | { type: "thinking"; text: string; encrypted: boolean }
  | { type: "marker"; text: string };

function asRecord(value: unknown): RawRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as RawRecord
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function providerEventPayload(event: Record<string, unknown>): RawRecord | null {
  return asRecord(event.payload);
}

function rawContentText(value: unknown): string | null {
  const direct = stringValue(value);
  if (direct) return direct;
  if (!Array.isArray(value)) return null;
  const texts = value.flatMap((blockValue) => {
    const block = asRecord(blockValue);
    const text = block?.type === "text" ? stringValue(block.text) : null;
    return text ? [text] : [];
  });
  return texts.length ? texts.join("\n") : null;
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
  for (const key of ["command", "file_path", "filePath", "path", "pattern", "query", "url", "tool", "action"]) {
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
  if (["grep", "glob", "search", "websearch"].includes(normalized)) return "search";
  if (["task", "agent", "collabtoolcall", "collabagenttoolcall"].includes(normalized)) return "agent";
  return "tool";
}

function codexToolName(item: RawRecord, itemType: string): string {
  if (itemType === "commandExecution") return "Bash";
  if (itemType === "mcpToolCall") {
    const server = stringValue(item.server) ?? "?";
    const tool = stringValue(item.tool) ?? "?";
    return `${itemType} ${server}.${tool}`;
  }
  return stringValue(item.tool) ?? stringValue(item.name) ?? itemType;
}

function codexToolInput(item: RawRecord, itemType: string): unknown {
  if (itemType === "commandExecution") return item.command ?? item.cmd ?? item.input;
  if (itemType === "fileChange") return item.changes ?? item.input ?? item.patch;
  if (itemType === "webSearch") return item.query ?? item.input;
  if (itemType === "imageView") return item.path ?? item.input;
  if (itemType === "collabToolCall" || itemType === "collabAgentToolCall") {
    return item.input ?? item.arguments ?? item.action;
  }
  return item.arguments ?? item.input ?? rawContentText(item.contentItems) ?? item.contentItems;
}

function codexToolTarget(item: RawRecord, input: unknown): string {
  return (
    stringValue(item.command) ||
    stringValue(item.query) ||
    stringValue(item.path) ||
    firstToolTarget(asRecord(input)) ||
    toolInputText(input)
  );
}

function providerToolOk(item: RawRecord, event: ReplayTimelineEvent | null): boolean | null {
  const result = asRecord(item.result);
  if (typeof result?.isError === "boolean") return !result.isError;
  if (typeof item.success === "boolean") return item.success;
  if (typeof item.exitCode === "number") return item.exitCode === 0;
  if (item.error !== null && item.error !== undefined) return false;
  const status = stringValue(item.status)?.toLowerCase();
  if (status === "failed" || status === "error") return false;
  if (["completed", "complete", "succeeded", "success", "done"].includes(status ?? "")) return true;
  return event?.bookmark === "error" ? false : null;
}

function codexToolFromItem(item: RawRecord, event: ReplayTimelineEvent | null): SessionTool | null {
  const itemType = stringValue(item.type) ?? "";
  const toolTypes = [
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "webSearch",
    "webSearchCall",
    "imageView",
    "collabToolCall",
    "collabAgentToolCall",
    "functionCall",
  ];
  if (!toolTypes.includes(itemType)) return null;
  const name = codexToolName(item, itemType);
  const inputValue = codexToolInput(item, itemType);
  const input = toolInputText(inputValue);
  const target = codexToolTarget(item, inputValue);
  return {
    name,
    input,
    output: null,
    ok: providerToolOk(item, event),
    archetype: archetypeForTool(name, itemType),
    summary: `${name.toLowerCase()}${target ? ` ${target}` : ""}`,
  };
}

function codexThinkingFromItem(item: RawRecord): PresentationBlock | null {
  if (item.type !== "reasoning") return null;
  const summaryText = Array.isArray(item.summary)
    ? item.summary
      .flatMap((value) => {
        const summary = asRecord(value);
        const text = summary ? stringValue(summary.text) : stringValue(value);
        return text ? [text] : [];
      })
      .join("\n")
    : null;
  const text = summaryText || rawContentText(item.content) || stringValue(item.text);
  return text ? { type: "thinking", text, encrypted: typeof item.encrypted_content === "string" } : null;
}

function claudeToolFromBlock(block: RawRecord): SessionTool | null {
  if (block.type === "tool_use") {
    const name = stringValue(block.name) ?? "tool";
    const input = toolInputText(block.input);
    const target = firstToolTarget(asRecord(block.input));
    return {
      name,
      input,
      output: null,
      ok: null,
      archetype: archetypeForTool(name),
      summary: `${name}${target ? ` ${target}` : ""}`,
    };
  }
  if (block.type !== "tool_result") return null;
  const resultText = rawContentText(block.content) ?? toolInputText(block.content);
  const nestedResult = asRecord(block.result);
  const isError = block.is_error === true || nestedResult?.isError === true;
  return {
    name: "tool result",
    input: resultText,
    output: null,
    ok: isError ? false : true,
    archetype: "tool",
    summary: `tool result${resultText ? ` ${resultText}` : ""}`,
  };
}

function claudeThinkingFromBlock(block: RawRecord): PresentationBlock | null {
  if (block.type !== "thinking") return null;
  const text = stringValue(block.thinking);
  return text ? { type: "thinking", text, encrypted: false } : null;
}

function markerBlock(event: ReplayTimelineEvent): PresentationBlock {
  const label = event.summary.trim() || event.kind.trim() || "unknown event";
  return { type: "marker", text: `event · ${label}` };
}

function summaryToolFromEvent(event: ReplayTimelineEvent): SessionTool | null {
  if (!/tool_use|tool_result|commandExecution/i.test(event.summary)) return null;
  const summary = event.summary.trim() || event.kind;
  const [name = event.kind] = summary.split(/\s+/, 2);
  return {
    name,
    input: "",
    output: null,
    ok: event.bookmark === "error" ? false : null,
    archetype: archetypeForTool(name),
    summary,
  };
}

export function providerEventToBlocks(
  payload: RawRecord | null,
  timelineEvent: ReplayTimelineEvent | null = null,
): PresentationBlock[] {
  if (!payload) {
    const summarizedTool = timelineEvent ? summaryToolFromEvent(timelineEvent) : null;
    if (summarizedTool) return [{ type: "tool", tool: summarizedTool }];
    if (timelineEvent && ["claude_user", "claude_assistant", "codex_user", "codex_assistant"].includes(timelineEvent.kind)) {
      return [{
        type: "message",
        role: timelineEvent.kind.includes("user") ? "user" : "assistant",
        text: timelineEvent.summary,
      }];
    }
    return timelineEvent ? [markerBlock(timelineEvent)] : [];
  }

  const item = codexItem(payload);
  if (item) {
    const itemType = stringValue(item.type) ?? "";
    if (itemType === "userMessage" || itemType === "agentMessage") {
      const role = itemType === "userMessage" ? "user" : "assistant";
      const text = stringValue(item.text) ?? rawContentText(item.content);
      if (text) return [{ type: "message", role, text }];
    }
    const thinking = codexThinkingFromItem(item);
    if (thinking) return [thinking];
    const tool = codexToolFromItem(item, timelineEvent);
    if (tool) return [{ type: "tool", tool }];
  }

  const message = asRecord(payload?.message);
  const role = stringValue(message?.role);
  const content = message?.content;
  if (role === "user" || role === "assistant") {
    const blocks: PresentationBlock[] = [];
    if (Array.isArray(content)) {
      for (const value of content) {
        const block = asRecord(value);
        if (!block) continue;
        if (block.type === "text") {
          const text = stringValue(block.text);
          if (text) blocks.push({ type: "message", role, text });
          continue;
        }
        const thinking = claudeThinkingFromBlock(block);
        if (thinking) {
          blocks.push(thinking);
          continue;
        }
        const tool = claudeToolFromBlock(block);
        if (tool) blocks.push({ type: "tool", tool });
      }
    } else {
      const text = rawContentText(content);
      if (text) blocks.push({ type: "message", role, text });
    }
    if (blocks.length) return blocks;
  }

  const summarizedTool = timelineEvent ? summaryToolFromEvent(timelineEvent) : null;
  if (summarizedTool) return [{ type: "tool", tool: summarizedTool }];

  if (timelineEvent && ["claude_user", "claude_assistant", "codex_user", "codex_assistant"].includes(timelineEvent.kind)) {
    return [{
      type: "message",
      role: timelineEvent.kind.includes("user") ? "user" : "assistant",
      text: timelineEvent.summary,
    }];
  }
  if (timelineEvent) return [markerBlock(timelineEvent)];
  const label = stringValue(payload.method) ?? stringValue(payload.type) ?? "unknown event";
  return [{ type: "marker", text: `event · ${label}` }];
}
