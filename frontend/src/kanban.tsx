import { useMemo, useRef, useState } from "react";
import type { CSSProperties, DragEvent, KeyboardEvent } from "react";
import { CheckCircle2, Plus } from "lucide-react";
import { ObsidianMarkdown } from "./markdown";
import type { NoteSummary } from "./types";

export type KanbanCard = {
  start: number;
  end: number;
  text: string;
};

export type KanbanLane = {
  title: string;
  headerLine: number;
  cards: KanbanCard[];
};

export function parseKanbanLanes(content: string): KanbanLane[] {
  const lines = content.split("\n");
  const lanes: KanbanLane[] = [];
  let current: KanbanLane | null = null;
  let index = 0;

  if (lines[0]?.trim() === "---") {
    for (index = 1; index < lines.length; index += 1) {
      if (lines[index].trim() === "---") {
        index += 1;
        break;
      }
    }
  }

  let inFence = false;
  for (; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;

    const heading = line.match(/^#{1,6}\s+(.+?)\s*$/);
    const label = line.match(/^([^-*\s#>][^:]*):\s*$/);
    const item = line.match(/^[-*]\s+(.+)$/);

    if (heading || label) {
      current = {
        title: (heading?.[1] ?? label?.[1] ?? "").trim(),
        headerLine: index,
        cards: []
      };
      lanes.push(current);
    } else if (item && current) {
      current.cards.push({ start: index, end: index, text: item[1] });
    } else if (current && current.cards.length > 0 && /^\s{2,}\S/.test(line)) {
      const last = current.cards[current.cards.length - 1];
      if (last.end === index - 1) {
        last.end = index;
        last.text += ` ${line.trim()}`;
      }
    }
  }

  return lanes;
}

function insertionLine(lane: KanbanLane, cardIndex: number, lines: string[]): number {
  if (cardIndex < lane.cards.length) return lane.cards[cardIndex].start;
  if (lane.cards.length > 0) return lane.cards[lane.cards.length - 1].end + 1;

  let at = lane.headerLine + 1;
  if (lines[at]?.trim() === "") at += 1;
  return at;
}

export function moveCard(
  content: string,
  card: { start: number; end: number },
  lane: KanbanLane,
  cardIndex: number
): string {
  const lines = content.split("\n");
  const block = lines.slice(card.start, card.end + 1);
  let insertAt = insertionLine(lane, cardIndex, lines);
  const blockLength = card.end - card.start + 1;

  if (insertAt >= card.start && insertAt <= card.end + 1) return content;

  lines.splice(card.start, blockLength);
  if (insertAt > card.start) insertAt -= blockLength;
  lines.splice(insertAt, 0, ...block);

  return lines.join("\n");
}

export function appendDoneEntry(content: string, text: string, date: string): string {
  const entry = `- ${text}`;
  const header = `## ${date}`;
  const lines = content.split("\n");

  let start = 0;
  if (lines[0]?.trim() === "---") {
    for (let i = 1; i < lines.length; i += 1) {
      if (lines[i].trim() === "---") {
        start = i + 1;
        break;
      }
    }
  }

  let firstH2 = -1;
  for (let i = start; i < lines.length; i += 1) {
    if (lines[i].startsWith("## ")) {
      firstH2 = i;
      break;
    }
  }

  if (firstH2 !== -1 && lines[firstH2].trim() === header) {
    let at = firstH2 + 1;
    if (lines[at]?.trim() === "") at += 1;
    lines.splice(at, 0, entry);
  } else {
    const at = firstH2 === -1 ? lines.length : firstH2;
    lines.splice(at, 0, header, "", entry, "");
  }

  return lines.join("\n");
}

export function addCard(content: string, lane: KanbanLane, text: string): string {
  const lines = content.split("\n");
  const insertAt = insertionLine(lane, lane.cards.length, lines);
  lines.splice(insertAt, 0, `- ${text}`);
  return lines.join("\n");
}

export function replaceCard(
  content: string,
  card: { start: number; end: number },
  text: string
): string {
  const lines = content.split("\n");
  lines.splice(card.start, card.end - card.start + 1, `- ${text}`);
  return lines.join("\n");
}

export function KanbanBoard({
  content,
  notes,
  onChange,
  onComplete,
  onOpenNote
}: {
  content: string;
  notes: NoteSummary[];
  onChange: (next: string) => void;
  onComplete: (card: KanbanCard) => void;
  onOpenNote: (path: string) => void;
}) {
  const lanes = useMemo(() => parseKanbanLanes(content), [content]);
  const draggingRef = useRef<KanbanCard | null>(null);
  const [dragging, setDragging] = useState<KanbanCard | null>(null);
  const [dropHint, setDropHint] = useState<{ lane: string; index: number } | null>(null);
  const [doneHover, setDoneHover] = useState(false);
  const [addingLane, setAddingLane] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ start: number; end: number; text: string } | null>(
    null
  );

  if (lanes.length === 0) {
    return <div className="kanban-empty">Nothing on the board.</div>;
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>, laneTitle: string) {
    if (!draggingRef.current) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";

    const container = event.currentTarget;
    const cards = [...container.querySelectorAll<HTMLElement>(":scope > .kanban-card")];
    const hovered = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest<HTMLElement>(".kanban-card");

    let index = cards.length;
    if (hovered && cards.includes(hovered)) {
      const rect = hovered.getBoundingClientRect();
      const position = cards.indexOf(hovered);
      const columns = getComputedStyle(container).gridTemplateColumns.split(" ").length;
      const after =
        columns > 1
          ? event.clientX > rect.left + rect.width / 2
          : event.clientY > rect.top + rect.height / 2;
      index = after ? position + 1 : position;
    }

    setDropHint((prev) =>
      prev && prev.lane === laneTitle && prev.index === index ? prev : { lane: laneTitle, index }
    );
  }

  function handleDrop(event: DragEvent<HTMLDivElement>, laneTitle: string) {
    event.preventDefault();
    const drag = draggingRef.current;
    const lane = lanes.find((entry) => entry.title === laneTitle);
    if (!drag || !lane) return;
    const index = dropHint?.lane === laneTitle ? dropHint.index : lane.cards.length;
    const next = moveCard(content, drag, lane, index);
    draggingRef.current = null;
    setDragging(null);
    setDropHint(null);
    if (next !== content) onChange(next);
  }

  function commitEdit() {
    const edit = editing;
    setEditing(null);
    if (!edit) return;
    const text = edit.text.trim();
    if (!text) return;
    const next = replaceCard(content, edit, text);
    if (next !== content) onChange(next);
  }

  function commitDraft(lane: KanbanLane) {
    const text = draft.trim();
    setAddingLane(null);
    setDraft("");
    if (text) onChange(addCard(content, lane, text));
  }

  function handleDraftKeyDown(event: KeyboardEvent<HTMLTextAreaElement>, lane: KanbanLane) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      commitDraft(lane);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setAddingLane(null);
      setDraft("");
    }
  }

  return (
    <div className="kanban-view">
      <header className="app-page-header kanban-page-header">
        <div className="app-page-header__leading">
          <span className="app-page-header__breadcrumb-segment is-current">Kanban</span>
        </div>
      </header>
      <div className="kanban-board">
      {dragging ? (
        <div
          className={`kanban-done-zone${doneHover ? " is-active" : ""}`}
          onDragLeave={() => setDoneHover(false)}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            setDoneHover(true);
          }}
          onDrop={(event) => {
            event.preventDefault();
            const card = draggingRef.current;
            draggingRef.current = null;
            setDragging(null);
            setDropHint(null);
            setDoneHover(false);
            if (card) onComplete(card);
          }}
        >
          <CheckCircle2 size={15} />
          <span>
            Drop to complete — logs to <code>done.md</code>
          </span>
        </div>
      ) : null}
      {lanes.map((lane) => (
        <section
          aria-label={lane.title}
          className="kanban-column"
          key={lane.title}
          style={
            {
              "--lane-cols": Math.max(1, Math.min(4, Math.ceil(lane.cards.length / 6)))
            } as CSSProperties
          }
        >
          <header className="kanban-column-header">
            <span className="kanban-column-title">{lane.title}</span>
            <span className="kanban-column-count">{lane.cards.length}</span>
          </header>
          <div
            className={`kanban-cards${
              dropHint?.lane === lane.title && dropHint.index === lane.cards.length
                ? " drop-at-end"
                : ""
            }`}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                setDropHint((prev) => (prev?.lane === lane.title ? null : prev));
              }
            }}
            onDragOver={(event) => handleDragOver(event, lane.title)}
            onDrop={(event) => handleDrop(event, lane.title)}
          >
            {lane.cards.map((card, index) => {
              const isEditing = editing?.start === card.start && editing?.end === card.end;
              return (
                <article
                  className={`kanban-card${
                    dragging?.start === card.start ? " is-dragging" : ""
                  }${
                    dropHint?.lane === lane.title && dropHint.index === index
                      ? " drop-before"
                      : ""
                  }${isEditing ? " is-editing" : ""}`}
                  draggable={!isEditing}
                  key={`${lane.title}-${card.start}`}
                  onDoubleClick={() => {
                    if (!isEditing) setEditing({ start: card.start, end: card.end, text: card.text });
                  }}
                  onDragEnd={() => {
                    draggingRef.current = null;
                    setDragging(null);
                    setDropHint(null);
                  }}
                  onDragStart={(event) => {
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", card.text);
                    draggingRef.current = card;
                    setDragging(card);
                  }}
                >
                  {isEditing ? (
                    <textarea
                      autoFocus
                      className="kanban-edit-input"
                      value={editing.text}
                      onBlur={commitEdit}
                      onChange={(event) =>
                        setEditing((current) =>
                          current ? { ...current, text: event.target.value } : current
                        )
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey) {
                          event.preventDefault();
                          commitEdit();
                        } else if (event.key === "Escape") {
                          event.preventDefault();
                          setEditing(null);
                        }
                      }}
                    />
                  ) : (
                    <div className="markdown-preview-view kanban-card-content">
                      <ObsidianMarkdown content={card.text} notes={notes} onOpenNote={onOpenNote} />
                    </div>
                  )}
                </article>
              );
            })}
          </div>
          <footer className="kanban-column-footer">
            {addingLane === lane.title ? (
              <textarea
                autoFocus
                className="kanban-add-input"
                placeholder="Card text..."
                rows={2}
                value={draft}
                onBlur={() => commitDraft(lane)}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => handleDraftKeyDown(event, lane)}
              />
            ) : (
              <button
                className="kanban-add-button bb-button bb-button--ghost bb-button--sm"
                type="button"
                onClick={() => {
                  setAddingLane(lane.title);
                  setDraft("");
                }}
              >
                <Plus size={14} />
                <span>Add a card</span>
              </button>
            )}
          </footer>
        </section>
      ))}
      </div>
    </div>
  );
}
