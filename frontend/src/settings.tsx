import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { THEMES, type ThemeId } from "./themes";

export type MonoFontChoice = {
  label: string;
  family: string;
  stack: string;
  load?: () => Promise<unknown>;
};

const fontsourceLoaders = {
  jetbrainsMono: () =>
    Promise.all([
      import("@fontsource/jetbrains-mono/latin-400.css"),
      import("@fontsource/jetbrains-mono/latin-500.css"),
      import("@fontsource/jetbrains-mono/latin-600.css"),
    ]),
  geistMono: () =>
    Promise.all([
      import("@fontsource/geist-mono/latin-400.css"),
      import("@fontsource/geist-mono/latin-500.css"),
      import("@fontsource/geist-mono/latin-600.css"),
    ]),
  firaCode: () =>
    Promise.all([
      import("@fontsource/fira-code/latin-400.css"),
      import("@fontsource/fira-code/latin-500.css"),
      import("@fontsource/fira-code/latin-600.css"),
    ]),
  ibmPlexMono: () =>
    Promise.all([
      import("@fontsource/ibm-plex-mono/latin-400.css"),
      import("@fontsource/ibm-plex-mono/latin-500.css"),
      import("@fontsource/ibm-plex-mono/latin-600.css"),
    ]),
  sourceCodePro: () =>
    Promise.all([
      import("@fontsource/source-code-pro/latin-400.css"),
      import("@fontsource/source-code-pro/latin-500.css"),
      import("@fontsource/source-code-pro/latin-600.css"),
    ]),
  robotoMono: () =>
    Promise.all([
      import("@fontsource/roboto-mono/latin-400.css"),
      import("@fontsource/roboto-mono/latin-500.css"),
      import("@fontsource/roboto-mono/latin-600.css"),
    ]),
  inconsolata: () =>
    Promise.all([
      import("@fontsource/inconsolata/latin-400.css"),
      import("@fontsource/inconsolata/latin-500.css"),
      import("@fontsource/inconsolata/latin-600.css"),
    ]),
  spaceMono: () =>
    Promise.all([
      import("@fontsource/space-mono/latin-400.css"),
      import("@fontsource/space-mono/latin-700.css"),
    ]),
  victorMono: () =>
    Promise.all([
      import("@fontsource/victor-mono/latin-400.css"),
      import("@fontsource/victor-mono/latin-500.css"),
      import("@fontsource/victor-mono/latin-600.css"),
    ]),
  redHatMono: () =>
    Promise.all([
      import("@fontsource/red-hat-mono/latin-400.css"),
      import("@fontsource/red-hat-mono/latin-500.css"),
      import("@fontsource/red-hat-mono/latin-600.css"),
    ]),
  martianMono: () =>
    Promise.all([
      import("@fontsource/martian-mono/latin-400.css"),
      import("@fontsource/martian-mono/latin-500.css"),
      import("@fontsource/martian-mono/latin-600.css"),
    ]),
};

const SYSTEM_STACK_TAIL =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';

// Full candidate list. System entries are gated via document.fonts.check().
export const MONO_FONTS: MonoFontChoice[] = [
  {
    label: "JetBrains Mono",
    family: "JetBrains Mono",
    stack: `"JetBrains Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.jetbrainsMono,
  },
  {
    label: "Geist Mono",
    family: "Geist Mono",
    stack: `"Geist Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.geistMono,
  },
  {
    label: "Fira Code",
    family: "Fira Code",
    stack: `"Fira Code", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.firaCode,
  },
  {
    label: "IBM Plex Mono",
    family: "IBM Plex Mono",
    stack: `"IBM Plex Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.ibmPlexMono,
  },
  {
    label: "Source Code Pro",
    family: "Source Code Pro",
    stack: `"Source Code Pro", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.sourceCodePro,
  },
  {
    label: "Roboto Mono",
    family: "Roboto Mono",
    stack: `"Roboto Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.robotoMono,
  },
  {
    label: "Inconsolata",
    family: "Inconsolata",
    stack: `"Inconsolata", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.inconsolata,
  },
  {
    label: "Space Mono",
    family: "Space Mono",
    stack: `"Space Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.spaceMono,
  },
  {
    label: "Victor Mono",
    family: "Victor Mono",
    stack: `"Victor Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.victorMono,
  },
  {
    label: "Red Hat Mono",
    family: "Red Hat Mono",
    stack: `"Red Hat Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.redHatMono,
  },
  {
    label: "Martian Mono",
    family: "Martian Mono",
    stack: `"Martian Mono", ${SYSTEM_STACK_TAIL}`,
    load: fontsourceLoaders.martianMono,
  },
  { label: "Monaco", family: "Monaco", stack: `Monaco, ${SYSTEM_STACK_TAIL}` },
  { label: "Menlo", family: "Menlo", stack: `Menlo, ${SYSTEM_STACK_TAIL}` },
  { label: "Consolas", family: "Consolas", stack: `Consolas, ${SYSTEM_STACK_TAIL}` },
  { label: "Courier New", family: "Courier New", stack: `"Courier New", ${SYSTEM_STACK_TAIL}` },
  { label: "Andale Mono", family: "Andale Mono", stack: `"Andale Mono", ${SYSTEM_STACK_TAIL}` },
  { label: "PT Mono", family: "PT Mono", stack: `"PT Mono", ${SYSTEM_STACK_TAIL}` },
  { label: "Cascadia Code", family: "Cascadia Code", stack: `"Cascadia Code", ${SYSTEM_STACK_TAIL}` },
  { label: "Cascadia Mono", family: "Cascadia Mono", stack: `"Cascadia Mono", ${SYSTEM_STACK_TAIL}` },
  { label: "Hack", family: "Hack", stack: `Hack, ${SYSTEM_STACK_TAIL}` },
  { label: "Iosevka", family: "Iosevka", stack: `Iosevka, ${SYSTEM_STACK_TAIL}` },
  { label: "Anonymous Pro", family: "Anonymous Pro", stack: `"Anonymous Pro", ${SYSTEM_STACK_TAIL}` },
  {
    label: "System (SF Mono)",
    family: "ui-monospace",
    stack: `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Roboto Mono", monospace`,
  },
];

const MONO_FONT_KEY = "wiki-mono-font";
const BODY_SIZE_KEY = "wiki-font-size-body";
const UI_SIZE_KEY = "wiki-font-size-ui";
const BODY_SIZE_DEFAULT = 16.5;
const UI_SIZE_DEFAULT = 13.5;
const PREVIEW_SAMPLE = "→ const x = 0O1lIi";

const loadedFonts = new Set<string>();
function loadFont(choice: MonoFontChoice): Promise<void> {
  if (!choice.load) return Promise.resolve();
  if (loadedFonts.has(choice.label)) return Promise.resolve();
  loadedFonts.add(choice.label);
  return choice.load().catch(() => {
    loadedFonts.delete(choice.label);
  }) as Promise<void>;
}

// document.fonts.check() is optimistic — returns true for anything, so it can't
// tell an installed system font from a missing one. Fall back to canvas metrics:
// if "family" renders identically under monospace and serif fallbacks, the font
// is actually installed; otherwise the fallbacks kicked in.
const AVAIL_SAMPLE = "mmmmmmmmwwwwwwwlliOoZ01";
const AVAIL_CACHE = new Map<string, boolean>();

function isFontInstalled(family: string): boolean {
  const cached = AVAIL_CACHE.get(family);
  if (cached !== undefined) return cached;
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return true;
    const measure = (spec: string) => {
      ctx.font = `72px ${spec}`;
      return ctx.measureText(AVAIL_SAMPLE).width;
    };
    const quoted = `"${family}"`;
    const installed =
      measure(`${quoted}, monospace`) === measure(`${quoted}, serif`) &&
      measure(`${quoted}, monospace`) === measure(`${quoted}, sans-serif`);
    AVAIL_CACHE.set(family, installed);
    return installed;
  } catch {
    return true;
  }
}

function isAvailable(choice: MonoFontChoice): boolean {
  // Bundled fontsource fonts are always available once loaded; keep them.
  if (choice.load) return true;
  if (choice.family === "ui-monospace") return true;
  return isFontInstalled(choice.family);
}

function applySizes(body: number, ui: number) {
  const root = document.documentElement.style;
  root.setProperty("--font-text-size", `${body}px`);
  root.setProperty("--font-ui-small", `${ui}px`);
  root.setProperty("--font-ui-smaller", `${ui - 1}px`);
}

function storedSize(key: string, fallback: number): number {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw >= 10 && raw <= 24 ? raw : fallback;
}

export function applyStoredMonoFont() {
  const stored = localStorage.getItem(MONO_FONT_KEY);
  const choice = MONO_FONTS.find((font) => font.label === stored) ?? MONO_FONTS[0];
  document.documentElement.style.setProperty("--font-monospace", choice.stack);
  void loadFont(choice);
  applySizes(
    storedSize(BODY_SIZE_KEY, BODY_SIZE_DEFAULT),
    storedSize(UI_SIZE_KEY, UI_SIZE_DEFAULT)
  );
}

export function currentMonoFont(): string {
  return localStorage.getItem(MONO_FONT_KEY) ?? MONO_FONTS[0].label;
}

function setMonoFont(label: string) {
  const choice = MONO_FONTS.find((font) => font.label === label) ?? MONO_FONTS[0];
  localStorage.setItem(MONO_FONT_KEY, choice.label);
  document.documentElement.style.setProperty("--font-monospace", choice.stack);
  void loadFont(choice);
}

function MonoFontPicker({
  current,
  onChange,
}: {
  current: string;
  onChange: (label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [availTick, setAvailTick] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const currentChoice = useMemo(
    () => MONO_FONTS.find((font) => font.label === current) ?? MONO_FONTS[0],
    [current]
  );

  useEffect(() => {
    if (!open) return;
    void Promise.all(MONO_FONTS.map(loadFont)).then(() => setAvailTick((t) => t + 1));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const visible = useMemo(
    () => MONO_FONTS.filter((font) => font.label === current || isAvailable(font)),
    // availTick invalidates the memo after loads land
    [current, availTick]
  );

  return (
    <div className={`mono-font-picker${open ? " is-open" : ""}`} ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        className="mono-font-trigger"
        type="button"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="mono-font-trigger-label" style={{ fontFamily: currentChoice.stack }}>
          {currentChoice.label}
        </span>
        <ChevronDown aria-hidden className="mono-font-trigger-chevron" size={14} />
      </button>
      {open ? (
        <div className="mono-font-menu" role="listbox">
          {visible.map((font) => {
            const active = font.label === current;
            return (
              <button
                aria-selected={active}
                className={`mono-font-option${active ? " is-active" : ""}`}
                key={font.label}
                role="option"
                type="button"
                onClick={() => {
                  onChange(font.label);
                  setOpen(false);
                }}
              >
                <span className="mono-font-option-label" style={{ fontFamily: font.stack }}>
                  {font.label}
                </span>
                <span
                  aria-hidden
                  className="mono-font-option-sample"
                  style={{ fontFamily: font.stack }}
                >
                  {PREVIEW_SAMPLE}
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

export function SettingsModal({
  onClose,
  onThemeChange,
  theme,
}: {
  onClose: () => void;
  onThemeChange: (theme: ThemeId) => void;
  theme: ThemeId;
}) {
  const [monoFont, setMonoFontState] = useState(currentMonoFont);
  const [bodySize, setBodySize] = useState(() => storedSize(BODY_SIZE_KEY, BODY_SIZE_DEFAULT));
  const [uiSize, setUiSize] = useState(() => storedSize(UI_SIZE_KEY, UI_SIZE_DEFAULT));

  function updateSizes(body: number, ui: number) {
    setBodySize(body);
    setUiSize(ui);
    localStorage.setItem(BODY_SIZE_KEY, String(body));
    localStorage.setItem(UI_SIZE_KEY, String(ui));
    applySizes(body, ui);
  }

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <>
      <div className="settings-backdrop" onClick={onClose} />
      <div aria-modal className="dialog settings-modal" role="dialog">
        <div className="settings-header">
          <div className="dialog-title">Settings</div>
          <button aria-label="Close settings" className="session-close" type="button" onClick={onClose}>
            <X size={14} />
          </button>
        </div>
        <div className="settings-section">
          <div className="settings-row settings-row-stack">
            <div className="settings-row-info">
              <div className="settings-row-name">Theme</div>
              <div className="settings-row-desc">
                UI chrome, diff accents, and syntax highlighting follow the selected palette.
              </div>
            </div>
            <div aria-label="Theme" className="theme-grid" role="radiogroup">
              {THEMES.map((option) => (
                <button
                  key={option.id}
                  aria-checked={theme === option.id}
                  className={`theme-choice${theme === option.id ? " is-active" : ""}`}
                  role="radio"
                  type="button"
                  onClick={() => onThemeChange(option.id)}
                >
                  <span aria-hidden className="theme-choice-swatches">
                    {option.preview.map((color, index) => (
                      <span
                        key={`${option.id}-${index}`}
                        className="theme-choice-swatch"
                        style={{ backgroundColor: color }}
                      />
                    ))}
                  </span>
                  <span className="theme-choice-name">{option.label}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Monospace font</div>
              <div className="settings-row-desc">
                Used for headings, code, agent transcripts, and UI chrome.
              </div>
            </div>
            <MonoFontPicker
              current={monoFont}
              onChange={(label) => {
                setMonoFont(label);
                setMonoFontState(label);
              }}
            />
          </div>
          <div className="settings-preview" style={{ fontFamily: "var(--font-monospace)" }}>
            wiki agent register PHO-1234 --orch phoebe {"->"} 0O1lI| fi ff
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">Note font size</div>
              <div className="settings-row-desc">Body text in notes and rendered transcripts.</div>
            </div>
            <div className="settings-slider">
              <input
                max={20}
                min={13}
                step={0.5}
                type="range"
                value={bodySize}
                onChange={(event) => updateSizes(Number(event.target.value), uiSize)}
              />
              <span className="settings-slider-value tabular-nums">{bodySize}px</span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-info">
              <div className="settings-row-name">UI font size</div>
              <div className="settings-row-desc">
                Chrome, chips, tool rows, sidebars (smaller variant follows at −1px).
              </div>
            </div>
            <div className="settings-slider">
              <input
                max={16}
                min={11}
                step={0.5}
                type="range"
                value={uiSize}
                onChange={(event) => updateSizes(bodySize, Number(event.target.value))}
              />
              <span className="settings-slider-value tabular-nums">{uiSize}px</span>
            </div>
          </div>
          <button
            className="settings-reset"
            type="button"
            onClick={() => updateSizes(BODY_SIZE_DEFAULT, UI_SIZE_DEFAULT)}
          >
            Reset sizes to default
          </button>
        </div>
      </div>
    </>
  );
}
