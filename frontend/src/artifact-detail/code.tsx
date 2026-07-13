import { useMemo } from "react";
import { ChevronsDownUp, ChevronsUpDown, Search, X } from "lucide-react";
import type { SessionArtifact } from "../api";
import type { ArtifactViewState } from "../transcript-store";

type FoldBlock = { start: number; end: number };

function indentation(line: string) {
  return line.match(/^\s*/)?.[0].replaceAll("\t", "  ").length ?? 0;
}

function findFoldBlocks(lines: string[]): FoldBlock[] {
  const blocks: FoldBlock[] = [];
  let depth = 0;
  let braceStart: number | null = null;
  lines.forEach((line, index) => {
    const opens = (line.match(/[\{\[\(]/g) ?? []).length;
    const closes = (line.match(/[\}\]\)]/g) ?? []).length;
    if (depth === 0 && opens > closes) braceStart = index;
    depth += opens - closes;
    if (depth <= 0) {
      if (braceStart !== null && index > braceStart + 1) blocks.push({ start: braceStart, end: index });
      braceStart = null;
      depth = 0;
    }
  });
  for (let index = 0; index < lines.length - 1; index += 1) {
    if (!lines[index].trim()) continue;
    const base = indentation(lines[index]);
    let next = index + 1;
    while (next < lines.length && !lines[next].trim()) next += 1;
    if (next >= lines.length || indentation(lines[next]) <= base) continue;
    let end = next;
    while (end + 1 < lines.length && (!lines[end + 1].trim() || indentation(lines[end + 1]) > base)) end += 1;
    if (end > index + 1) blocks.push({ start: index, end });
  }
  return [...new Map(blocks.map((block) => [block.start, block])).values()].sort((left, right) => left.start - right.start);
}

function HighlightedLine({ line, needle }: { line: string; needle: string }) {
  if (!needle) return <>{line || " "}</>;
  const lower = line.toLocaleLowerCase();
  const wanted = needle.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  let match = lower.indexOf(wanted);
  while (match >= 0) {
    parts.push(line.slice(cursor, match));
    parts.push(<mark key={match}>{line.slice(match, match + needle.length)}</mark>);
    cursor = match + needle.length;
    match = lower.indexOf(wanted, cursor);
  }
  parts.push(line.slice(cursor));
  return <>{parts}</>;
}

export function CodeArtifactDetail({
  artifact,
  onChange,
  state,
}: {
  artifact: SessionArtifact;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
}) {
  const lines = (artifact.source ?? "").split("\n");
  const blocks = useMemo(() => findFoldBlocks(lines), [artifact.source]);
  const blockByStart = new Map(blocks.map((block) => [block.start, block]));
  const folded = new Set(state.foldedBlocks ?? []);
  const find = state.find ?? "";
  const matches = find ? (artifact.source ?? "").toLocaleLowerCase().split(find.toLocaleLowerCase()).length - 1 : 0;

  function update(next: Partial<ArtifactViewState>) {
    onChange({ ...state, ...next });
  }

  function toggleFold(start: number) {
    const next = new Set(folded);
    if (next.has(start)) next.delete(start);
    else next.add(start);
    update({ foldedBlocks: [...next].sort((left, right) => left - right) });
  }

  const rendered = [];
  for (let index = 0; index < lines.length; index += 1) {
    const containing = blocks.find((block) => folded.has(block.start) && index > block.start && index <= block.end);
    if (containing) {
      if (index === containing.start + 1) {
        rendered.push(<div className="artifact-code-fold-marker" key={`fold-${containing.start}`}>… {containing.end - containing.start} lines folded</div>);
      }
      continue;
    }
    const block = blockByStart.get(index);
    rendered.push(
      <div className="artifact-code-line" key={index}>
        <span className="artifact-code-gutter">
          {block ? <button aria-label={`${folded.has(index) ? "Expand" : "Collapse"} block at line ${index + 1}`} type="button" onClick={() => toggleFold(index)}>{folded.has(index) ? "+" : "−"}</button> : null}
          <span>{index + 1}</span>
        </span>
        <code><HighlightedLine line={lines[index]} needle={find} /></code>
      </div>
    );
  }

  return (
    <div className="artifact-code-detail">
      <div className="artifact-detail-toolbar">
        <button type="button" onClick={() => update({ foldedBlocks: blocks.map((block) => block.start) })}><ChevronsDownUp size={12} /> Collapse all</button>
        <button type="button" onClick={() => update({ foldedBlocks: [] })}><ChevronsUpDown size={12} /> Expand all</button>
        <button data-code-find="true" type="button" onClick={() => update({ findOpen: true })}><Search size={12} /> Find</button>
        {state.findOpen ? (
          <label className="artifact-code-find">
            <Search size={12} />
            <input autoFocus aria-label="Find in code" value={find} onChange={(event) => update({ find: event.target.value })} />
            <span>{matches} matches</span>
            <button aria-label="Close find" type="button" onClick={() => update({ findOpen: false, find: "" })}><X size={11} /></button>
          </label>
        ) : null}
      </div>
      {artifact.filename ? <div className="artifact-code-detail-filename">{artifact.filename}</div> : null}
      <pre className="artifact-code-detail-source" data-language={artifact.language}>{rendered}</pre>
    </div>
  );
}
