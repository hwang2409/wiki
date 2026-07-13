import { useMemo, useState } from "react";
import { Copy, Search } from "lucide-react";
import type { ArtifactColumn, SessionArtifact } from "../api";
import type { ArtifactViewState } from "../transcript-store";

type Row = (string | number | boolean | null)[];
type CopyFormat = "tsv" | "csv" | "json";

function compareCells(left: unknown, right: unknown, column: ArtifactColumn): number {
  if (column.type === "number") return Number(left ?? 0) - Number(right ?? 0);
  if (column.type === "date") return new Date(String(left ?? "")).getTime() - new Date(String(right ?? "")).getTime();
  return String(left ?? "").localeCompare(String(right ?? ""), undefined, { numeric: true, sensitivity: "base" });
}

function csvCell(value: unknown) {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function rowsText(columns: ArtifactColumn[], rows: Row[], format: CopyFormat) {
  if (format === "json") {
    return JSON.stringify(rows.map((row) => Object.fromEntries(columns.map((column, index) => [column.key, row[index] ?? null]))), null, 2);
  }
  const separator = format === "tsv" ? "\t" : ",";
  const encode = format === "tsv" ? (value: unknown) => String(value ?? "") : csvCell;
  return [
    columns.map((column) => encode(column.label)).join(separator),
    ...rows.map((row) => row.map(encode).join(separator)),
  ].join("\n");
}

function safeLink(value: string) {
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : null;
  } catch {
    return null;
  }
}

export function TableArtifactDetail({
  artifact,
  onChange,
  state,
}: {
  artifact: SessionArtifact;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
}) {
  const columns = artifact.columns ?? [];
  const rows = artifact.rows ?? [];
  const [format, setFormat] = useState<CopyFormat>("tsv");
  const [copied, setCopied] = useState("");
  const filter = state.filter ?? "";
  const filteredRows = useMemo(() => {
    const needle = filter.trim().toLocaleLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => row.some((cell) => typeof cell === "string" && cell.toLocaleLowerCase().includes(needle)));
  }, [filter, rows]);
  const visibleRows = useMemo(() => {
    if (!state.sortColumn || !state.sortDirection) return filteredRows;
    const index = columns.findIndex((column) => column.key === state.sortColumn);
    const column = columns[index];
    if (index < 0 || !column) return filteredRows;
    const direction = state.sortDirection === "asc" ? 1 : -1;
    return filteredRows
      .map((row, originalIndex) => ({ row, originalIndex }))
      .sort((left, right) => compareCells(left.row[index], right.row[index], column) * direction || left.originalIndex - right.originalIndex)
      .map(({ row }) => row);
  }, [columns, filteredRows, state.sortColumn, state.sortDirection]);

  function update(next: Partial<ArtifactViewState>) {
    onChange({ ...state, ...next });
  }

  async function copy(text: string, label: string) {
    await navigator.clipboard.writeText(text);
    setCopied(label);
    window.setTimeout(() => setCopied(""), 1200);
  }

  function cycleSort(column: ArtifactColumn) {
    if (state.sortColumn !== column.key || !state.sortDirection) update({ sortColumn: column.key, sortDirection: "asc" });
    else if (state.sortDirection === "asc") update({ sortDirection: "desc" });
    else update({ sortColumn: null, sortDirection: null });
  }

  return (
    <div className="artifact-table-detail">
      <div className="artifact-detail-toolbar artifact-table-toolbar">
        <label className="artifact-filter">
          <Search size={12} />
          <input aria-label="Filter table" placeholder="Filter string cells" value={filter} onChange={(event) => update({ filter: event.target.value })} />
        </label>
        <span className="artifact-table-result-count tabular-nums">{visibleRows.length} / {rows.length} rows</span>
        <select aria-label="Copy format" value={format} onChange={(event) => setFormat(event.target.value as CopyFormat)}>
          <option value="tsv">TSV</option>
          <option value="csv">CSV</option>
          <option value="json">JSON</option>
        </select>
        <button type="button" onClick={() => void copy(rowsText(columns, visibleRows, format), "visible")}><Copy size={12} /> {copied === "visible" ? "Copied" : "Copy visible"}</button>
        <button type="button" onClick={() => void copy(rowsText(columns, rows, format), "all")}><Copy size={12} /> {copied === "all" ? "Copied" : "Copy all"}</button>
      </div>
      <div className="artifact-table-detail-scroll">
        <table className="artifact-table tabular-nums">
          <thead>
            <tr>
              <th aria-label="Row actions" />
              {columns.map((column, columnIndex) => {
                const active = state.sortColumn === column.key && state.sortDirection;
                return (
                  <th className={`is-${column.type}`} key={column.key}>
                    <div className="artifact-table-detail-heading">
                      <button aria-label={`Sort by ${column.label}`} type="button" onClick={() => cycleSort(column)}>
                        {column.label}
                        {active ? <span aria-hidden="true">{state.sortDirection === "asc" ? "↑" : "↓"}</span> : null}
                      </button>
                      <button
                        aria-label={`Copy ${column.label} column`}
                        className="artifact-table-copy-column"
                        title={`Copy ${column.label} column`}
                        type="button"
                        onClick={() => void copy(visibleRows.map((row) => String(row[columnIndex] ?? "")).join("\n"), `column-${column.key}`)}
                      ><Copy size={11} /></button>
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, rowIndex) => (
              <tr key={`${rowIndex}:${row.join("\u0000")}`}>
                <td className="artifact-table-row-action">
                  <button aria-label={`Copy row ${rowIndex + 1}`} type="button" onClick={() => void copy(row.map((cell) => String(cell ?? "")).join("\t"), `row-${rowIndex}`)}><Copy size={11} /></button>
                </td>
                {columns.map((column, columnIndex) => {
                  const value = row[columnIndex];
                  const link = column.type === "link" && typeof value === "string" ? safeLink(value) : null;
                  return (
                    <td className={`is-${column.type}`} key={column.key}>
                      {link ? <a href={link} rel="noreferrer" target="_blank">{value}</a> : String(value ?? "")}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
