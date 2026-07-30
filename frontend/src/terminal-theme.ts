import type { ITheme } from "@xterm/xterm";

type Rgba = {
  r: number;
  g: number;
  b: number;
  a: number;
};

export type DerivedTerminalTheme = {
  chromeVars: Record<string, string>;
  renderer: ITheme;
};

function readVar(styles: CSSStyleDeclaration, name: string) {
  return styles.getPropertyValue(name).trim();
}

function clampChannel(value: number) {
  return Math.max(0, Math.min(255, Math.round(value)));
}

function parseHex(value: string): Rgba | null {
  const normalized = value.replace("#", "").trim();
  if (normalized.length === 3) {
    return {
      r: parseInt(normalized[0] + normalized[0], 16),
      g: parseInt(normalized[1] + normalized[1], 16),
      b: parseInt(normalized[2] + normalized[2], 16),
      a: 1,
    };
  }
  if (normalized.length === 6 || normalized.length === 8) {
    return {
      r: parseInt(normalized.slice(0, 2), 16),
      g: parseInt(normalized.slice(2, 4), 16),
      b: parseInt(normalized.slice(4, 6), 16),
      a: normalized.length === 8 ? parseInt(normalized.slice(6, 8), 16) / 255 : 1,
    };
  }
  return null;
}

function parseRgb(value: string): Rgba | null {
  const match = value.match(/rgba?\(([^)]+)\)/i);
  if (!match) return null;
  const parts = match[1].split(",").map((part) => Number.parseFloat(part.trim()));
  if (parts.length < 3 || parts.some((part) => Number.isNaN(part))) return null;
  return {
    r: clampChannel(parts[0]),
    g: clampChannel(parts[1]),
    b: clampChannel(parts[2]),
    a: parts.length >= 4 && !Number.isNaN(parts[3]) ? Math.max(0, Math.min(1, parts[3])) : 1,
  };
}

function parseColor(value: string): Rgba {
  return parseHex(value) ?? parseRgb(value) ?? { r: 0, g: 0, b: 0, a: 1 };
}

function formatColor(color: Rgba) {
  const r = clampChannel(color.r);
  const g = clampChannel(color.g);
  const b = clampChannel(color.b);
  if (color.a >= 0.999) return `rgb(${r}, ${g}, ${b})`;
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, color.a)).toFixed(3)})`;
}

function mix(left: string, right: string, ratio: number) {
  const a = parseColor(left);
  const b = parseColor(right);
  const clamped = Math.max(0, Math.min(1, ratio));
  return formatColor({
    r: a.r + (b.r - a.r) * clamped,
    g: a.g + (b.g - a.g) * clamped,
    b: a.b + (b.b - a.b) * clamped,
    a: a.a + (b.a - a.a) * clamped,
  });
}

function withAlpha(value: string, alpha: number) {
  const color = parseColor(value);
  return formatColor({ ...color, a: Math.max(0, Math.min(1, alpha)) });
}

export function deriveTerminalTheme(target: HTMLElement = document.documentElement): DerivedTerminalTheme {
  const styles = getComputedStyle(target);
  const background = readVar(styles, "--background-primary");
  const backgroundAlt = readVar(styles, "--background-primary-alt");
  const surface = readVar(styles, "--background-secondary");
  const border = readVar(styles, "--background-modifier-border");
  const foreground = readVar(styles, "--text-normal");
  const muted = readVar(styles, "--text-muted");
  const faint = readVar(styles, "--text-faint");
  const accent = readVar(styles, "--accent-primary");
  const success = readVar(styles, "--accent-success");
  const warning = readVar(styles, "--accent-warning");
  const danger = readVar(styles, "--accent-danger");
  const syntaxKeyword = readVar(styles, "--syntax-keyword");
  const syntaxFunction = readVar(styles, "--syntax-function");
  const syntaxString = readVar(styles, "--syntax-string");
  const syntaxNumber = readVar(styles, "--syntax-number");
  const syntaxType = readVar(styles, "--syntax-type");
  const syntaxVariable = readVar(styles, "--syntax-variable");
  const scrollbar = readVar(styles, "--scrollbar-thumb");
  const scrollbarHover = readVar(styles, "--scrollbar-thumb-hover");

  return {
    chromeVars: {
      "--terminal-surface": background,
      "--terminal-surface-alt": mix(surface, backgroundAlt, 0.45),
      "--terminal-border": border,
      "--terminal-text": foreground,
      "--terminal-muted": muted,
      "--terminal-faint": faint,
      "--terminal-accent": accent,
      "--terminal-success": success,
      "--terminal-warning": warning,
      "--terminal-danger": danger,
      "--terminal-overlay": withAlpha(mix(background, surface, 0.45), 0.96),
    },
    renderer: {
      background,
      foreground,
      cursor: foreground,
      cursorAccent: background,
      selectionBackground: withAlpha(accent, 0.24),
      selectionInactiveBackground: withAlpha(muted, 0.16),
      scrollbarSliderBackground: withAlpha(scrollbar, 0.34),
      scrollbarSliderHoverBackground: withAlpha(scrollbarHover, 0.48),
      scrollbarSliderActiveBackground: withAlpha(scrollbarHover, 0.58),
      black: mix(backgroundAlt, foreground, 0.08),
      red: danger,
      green: success,
      yellow: warning,
      blue: accent,
      magenta: syntaxKeyword,
      cyan: syntaxType,
      white: muted,
      brightBlack: faint,
      brightRed: mix(danger, syntaxString, 0.3),
      brightGreen: mix(success, syntaxFunction, 0.3),
      brightYellow: mix(warning, syntaxNumber, 0.3),
      brightBlue: mix(accent, syntaxVariable, 0.25),
      brightMagenta: mix(syntaxKeyword, syntaxFunction, 0.35),
      brightCyan: mix(syntaxType, syntaxVariable, 0.35),
      brightWhite: foreground,
    },
  };
}
