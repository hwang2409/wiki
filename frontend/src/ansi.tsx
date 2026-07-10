import type { ReactNode } from "react";

export type AnsiStyle = {
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  fg?: number; // 0-7 base, 8-15 bright
  bg?: number;
};

export type AnsiSegment = { text: string; style: AnsiStyle };

function styleEqual(a: AnsiStyle, b: AnsiStyle): boolean {
  return (
    a.bold === b.bold &&
    a.italic === b.italic &&
    a.underline === b.underline &&
    a.fg === b.fg &&
    a.bg === b.bg
  );
}

function applySgr(style: AnsiStyle, params: number[]): AnsiStyle {
  let out: AnsiStyle = { ...style };
  let i = 0;
  while (i < params.length) {
    const p = params[i];
    if (p === 0) out = {};
    else if (p === 1) out.bold = true;
    else if (p === 3) out.italic = true;
    else if (p === 4) out.underline = true;
    else if (p === 22) delete out.bold;
    else if (p === 23) delete out.italic;
    else if (p === 24) delete out.underline;
    else if (p >= 30 && p <= 37) out.fg = p - 30;
    else if (p === 38) {
      // 256 (5;N) or truecolor (2;R;G;B) — swallow, no v0 support
      const kind = params[i + 1];
      if (kind === 5) i += 2;
      else if (kind === 2) i += 4;
      else i += 1;
    } else if (p === 39) delete out.fg;
    else if (p >= 40 && p <= 47) out.bg = p - 40;
    else if (p === 48) {
      const kind = params[i + 1];
      if (kind === 5) i += 2;
      else if (kind === 2) i += 4;
      else i += 1;
    } else if (p === 49) delete out.bg;
    else if (p >= 90 && p <= 97) out.fg = p - 90 + 8;
    else if (p >= 100 && p <= 107) out.bg = p - 100 + 8;
    i += 1;
  }
  return out;
}

const CSI_PARAM = /[0-9;?]/;

export function parseAnsi(text: string): AnsiSegment[] {
  const segments: AnsiSegment[] = [];
  let style: AnsiStyle = {};
  let buf = "";
  const flush = () => {
    if (!buf) return;
    const last = segments[segments.length - 1];
    if (last && styleEqual(last.style, style)) last.text += buf;
    else segments.push({ text: buf, style });
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    const code = text.charCodeAt(i);
    if (code !== 0x1b) {
      buf += text[i];
      i += 1;
      continue;
    }
    const next = text[i + 1];
    if (next === "[") {
      let j = i + 2;
      while (j < text.length && CSI_PARAM.test(text[j])) j += 1;
      const params = text.slice(i + 2, j);
      const final = text[j];
      if (final === "m") {
        flush();
        const nums = params.split(";").map((p) => (p === "" ? 0 : Number(p)));
        style = applySgr(style, nums);
      }
      // any other final byte (cursor / erase) — swallow, no output
      i = j < text.length ? j + 1 : j;
    } else if (next === "]") {
      // OSC — swallow until BEL or ESC \
      let j = i + 2;
      while (j < text.length) {
        if (text.charCodeAt(j) === 0x07) {
          j += 1;
          break;
        }
        if (text.charCodeAt(j) === 0x1b && text[j + 1] === "\\") {
          j += 2;
          break;
        }
        j += 1;
      }
      i = j;
    } else if (next === undefined) {
      i += 1;
    } else {
      // Short two-char ESC (e.g. ESC c, ESC =) — swallow both
      i += 2;
    }
  }
  flush();
  return segments;
}

function classNamesFor(style: AnsiStyle): string {
  const cs: string[] = [];
  if (style.bold) cs.push("ansi-bold");
  if (style.italic) cs.push("ansi-italic");
  if (style.underline) cs.push("ansi-underline");
  if (style.fg !== undefined) cs.push(`ansi-fg-${style.fg}`);
  if (style.bg !== undefined) cs.push(`ansi-bg-${style.bg}`);
  return cs.join(" ");
}

export function hasAnsi(text: string): boolean {
  return text.includes("\x1b");
}

export function renderAnsi(text: string): ReactNode {
  if (!hasAnsi(text)) return text;
  const segments = parseAnsi(text);
  return segments.map((seg, i) => {
    const cn = classNamesFor(seg.style);
    if (!cn) return seg.text;
    return (
      <span className={cn} key={i}>
        {seg.text}
      </span>
    );
  });
}
