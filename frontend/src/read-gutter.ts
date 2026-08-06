// WIKI-261: parse the numbered-payload gutter that read tools emit.
// Claude's Read tool prints `\t<n>\t<code>` (cat -n style); other harnesses
// use `<n>→<code>` or `<n>|<code>`. Prior to WIKI-261 the render layer just
// dropped the language hint for any output matching this shape — safer than
// tokenizing gutter-prefixed text but stripped highlighting even for pure
// numbered payloads. This module parses the payload into (num, code) pairs
// so the code column can be tokenized cleanly while the gutter renders as a
// muted, non-selectable rail.

export type NumberedLine = { num: number; text: string };

export type NumberedPayload = {
  lines: NumberedLine[];
  code: string;
};

// One line of a numbered payload. Accepts leading whitespace (cat -n pads
// numbers right-aligned), a run of digits, one separator, then the code.
// `→` is Claude's arrow separator; `|` covers common alt formats; `\t`
// covers real cat -n. `.*` in code is greedy but bounded to the line.
const LINE_PATTERN = /^\s*(\d+)[\t→|](.*)$/;

// A leading digit-then-separator on the very first non-blank line means the
// payload is a plausible candidate. Cheap gate — full parse validates every
// line before we commit to the numbered-rendering path.
const CHEAP_HINT = /^\s*\d+[\t→|]/;

// Trailing marker the harness appends to truncated read output — e.g.
// `… [1727 chars truncated]`. It's not part of the numbered content, so we
// pop it before the per-line validation. Keeping the marker would flip the
// parse to null and drop highlighting for every long file read.
const TRUNCATION_TAIL = /^\s*(?:…|\.\.\.)?\s*\[[^\]]*truncated[^\]]*\]\s*$/i;

export function looksLikeNumberedPayload(text: string): boolean {
  return CHEAP_HINT.test(text);
}

// Parse a payload. Returns null when ANY non-empty line fails the pattern
// (mixed payloads — e.g. a wrapper line, a diff header, or garbled output —
// fall back to the plain renderer instead of misaligning the gutter).
// Fully blank lines pass through as blank code with num=0; readers see the
// same blank row they'd see in a plain render, and the gutter reserves the
// column without inventing a fake line number.
export function parseNumberedPayload(text: string): NumberedPayload | null {
  if (!text) return null;
  if (!looksLikeNumberedPayload(text)) return null;
  const rawLines = text.split("\n");
  // Drop a single trailing newline artifact (final "\n" produces a bogus
  // empty tail line that shouldn't count against the parse).
  while (rawLines.length > 1 && rawLines[rawLines.length - 1] === "") {
    rawLines.pop();
  }
  // Drop the harness's truncation marker if it landed on the last line —
  // long file reads always end with one and it would otherwise fail the
  // per-line pattern and disable highlighting for every long payload.
  if (rawLines.length > 1 && TRUNCATION_TAIL.test(rawLines[rawLines.length - 1])) {
    rawLines.pop();
    while (rawLines.length > 1 && rawLines[rawLines.length - 1] === "") {
      rawLines.pop();
    }
  }
  const lines: NumberedLine[] = [];
  let sawNumbered = false;
  for (const raw of rawLines) {
    if (raw === "") {
      lines.push({ num: 0, text: "" });
      continue;
    }
    const match = LINE_PATTERN.exec(raw);
    if (!match) return null;
    const num = Number.parseInt(match[1], 10);
    if (!Number.isFinite(num)) return null;
    lines.push({ num, text: match[2] });
    sawNumbered = true;
  }
  if (!sawNumbered) return null;
  return { lines, code: lines.map((line) => line.text).join("\n") };
}

// Width (in characters) of the widest line number in the payload. Callers
// use this to size the gutter column so numbers stay right-aligned without
// paying a runtime measurement.
export function numberedGutterWidth(payload: NumberedPayload): number {
  let widest = 1;
  for (const line of payload.lines) {
    if (line.num === 0) continue;
    const digits = String(line.num).length;
    if (digits > widest) widest = digits;
  }
  return widest;
}
