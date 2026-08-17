import { useEffect, useRef } from "react";

import {
  COMMANDS,
  missingRequired,
  type ComposerCommand,
} from "./composer-commands";

export const SLASH_MENU_ID = "composer-slash-menu";
export const slashMenuOptionId = (index: number) =>
  `${SLASH_MENU_ID}-option-${index}`;

export type SlashMenuProps = {
  commands: readonly ComposerCommand[];
  activeIndex: number;
  onSelect: (command: ComposerCommand) => void;
  onHover: (index: number) => void;
};

export function SlashMenu({ commands, activeIndex, onSelect, onHover }: SlashMenuProps) {
  if (commands.length === 0) return null;
  return (
    <div
      className="composer-slash-menu"
      id={SLASH_MENU_ID}
      role="listbox"
      aria-label="Slash commands"
    >
      {commands.map((command, index) => (
        <button
          className={`bb-menu-row composer-slash-item${index === activeIndex ? " is-active" : ""}`}
          key={command.name}
          id={slashMenuOptionId(index)}
          role="option"
          aria-selected={index === activeIndex}
          type="button"
          onMouseEnter={() => onHover(index)}
          onMouseDown={(event) => {
            event.preventDefault();
            onSelect(command);
          }}
        >
          <span className="composer-slash-name prompt-mention-pill">/{command.name}</span>
          <span className="composer-slash-args">
            {command.args
              .map((arg) => (arg.required ? `<${arg.name}>` : `[${arg.name}]`))
              .join(" ")}
          </span>
          <span className="composer-slash-desc">{command.description}</span>
        </button>
      ))}
    </div>
  );
}

export type CommandFormProps = {
  command: ComposerCommand;
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  busy: boolean;
  error: string | null;
};

export function CommandForm({
  command,
  values,
  onChange,
  onSubmit,
  onCancel,
  busy,
  error,
}: CommandFormProps) {
  const firstRef = useRef<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null>(null);
  useEffect(() => {
    firstRef.current?.focus();
  }, [command.name]);

  const missing = missingRequired(command, values);
  const disabled = busy || missing.length > 0;
  const disabledReason = busy
    ? "sending…"
    : missing.length > 0
      ? `fill required: ${missing.map((arg) => arg.name).join(", ")}`
      : undefined;

  function handleKeyDown(event: React.KeyboardEvent) {
    // Escape only fires cancel when nothing is in-flight; while busy the
    // form stays open so the user isn't tricked into thinking cancel
    // aborted an already-dispatched destructive `/spawn` / `/archive`.
    // Round-7 REVIEW finding [MEDIUM] (composer-slash-menu.tsx:88).
    if (event.key === "Escape") {
      if (busy) return;
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Enter" || event.shiftKey || disabled) return;
    // Textareas swallow plain Enter for newlines; only submit when the
    // user adds Meta / Ctrl.
    const isTextarea = (event.target as HTMLElement).tagName === "TEXTAREA";
    const hasModifier = event.metaKey || event.ctrlKey;
    if (isTextarea && !hasModifier) return;
    // Round-7 REVIEW [HIGH]: submit exactly once. The prior code had two
    // sequential `if` branches so Meta+Enter on a non-textarea input hit
    // both and dispatched twice before React re-rendered with `busy`.
    event.preventDefault();
    onSubmit();
  }

  return (
    <form
      className="composer-command-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled) onSubmit();
      }}
      onKeyDown={handleKeyDown}
    >
      <div className="composer-command-header">
        <span className="composer-command-chip">/{command.name}</span>
        <span className="composer-command-desc">{command.description}</span>
        <button
          className="composer-command-cancel"
          type="button"
          onClick={onCancel}
          disabled={busy}
          title={busy ? "cannot cancel while sending" : "Cancel (Esc)"}
        >
          esc
        </button>
      </div>
      <div className="composer-command-args">
        {command.args.map((arg, index) => {
          const value = values[arg.name] ?? "";
          const missing = arg.required && !value.trim();
          const commonProps = {
            className: `composer-command-input${missing ? " is-missing" : ""}`,
            placeholder: arg.placeholder ?? arg.hint,
            value,
            onChange: (
              event: React.ChangeEvent<
                HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
              >,
            ) => onChange(arg.name, event.target.value),
            "aria-label": arg.name,
            "aria-required": arg.required,
          };
          return (
            <label className="composer-command-slot" key={arg.name}>
              <span className="composer-command-label">
                {arg.name}
                {arg.required ? "" : <span className="composer-command-optional"> · optional</span>}
              </span>
              {arg.type === "enum" ? (
                <select
                  {...commonProps}
                  ref={(node) => {
                    if (index === 0) firstRef.current = node;
                  }}
                >
                  {!arg.required && !value ? <option value=""></option> : null}
                  {(arg.options ?? []).map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : arg.type === "long-text" ? (
                <textarea
                  {...commonProps}
                  rows={3}
                  ref={(node) => {
                    if (index === 0) firstRef.current = node;
                  }}
                />
              ) : (
                <input
                  {...commonProps}
                  type="text"
                  autoComplete="off"
                  autoCorrect="off"
                  spellCheck={false}
                  ref={(node) => {
                    if (index === 0) firstRef.current = node;
                  }}
                />
              )}
              <span className="composer-command-hint">{arg.hint}</span>
            </label>
          );
        })}
      </div>
      {error ? <div className="composer-command-error">{error}</div> : null}
      <div className="composer-command-actions">
        <button
          className="composer-command-submit"
          type="submit"
          disabled={disabled}
          title={disabledReason ?? "Send (Enter)"}
        >
          {busy ? "sending…" : "run"}
        </button>
        <span className="composer-command-tip">
          tab · shift+tab to move · enter to run · esc to cancel
        </span>
      </div>
    </form>
  );
}

export { COMMANDS };
