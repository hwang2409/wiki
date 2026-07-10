import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { THEMES, type ThemeId } from "./themes";

export type FontChoice = {
  label: string;
  family: string;
  stack: string;
  load?: () => Promise<unknown>;
};

const monoLoaders = {
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

const proportionalLoaders = {
  inter: () =>
    Promise.all([
      import("@fontsource/inter/latin-400.css"),
      import("@fontsource/inter/latin-500.css"),
      import("@fontsource/inter/latin-600.css"),
    ]),
  sourceSerif4: () =>
    Promise.all([
      import("@fontsource/source-serif-4/latin-400.css"),
      import("@fontsource/source-serif-4/latin-600.css"),
    ]),
};

const MONO_TAIL =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const SANS_TAIL =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif';
const SERIF_TAIL =
  'Georgia, Charter, "Iowan Old Style", "Times New Roman", serif';

export const MONO_FONTS: FontChoice[] = [
  {
    label: "JetBrains Mono",
    family: "JetBrains Mono",
    stack: `"JetBrains Mono", ${MONO_TAIL}`,
    load: monoLoaders.jetbrainsMono,
  },
  {
    label: "Geist Mono",
    family: "Geist Mono",
    stack: `"Geist Mono", ${MONO_TAIL}`,
    load: monoLoaders.geistMono,
  },
  {
    label: "Fira Code",
    family: "Fira Code",
    stack: `"Fira Code", ${MONO_TAIL}`,
    load: monoLoaders.firaCode,
  },
  {
    label: "IBM Plex Mono",
    family: "IBM Plex Mono",
    stack: `"IBM Plex Mono", ${MONO_TAIL}`,
    load: monoLoaders.ibmPlexMono,
  },
  {
    label: "Source Code Pro",
    family: "Source Code Pro",
    stack: `"Source Code Pro", ${MONO_TAIL}`,
    load: monoLoaders.sourceCodePro,
  },
  {
    label: "Roboto Mono",
    family: "Roboto Mono",
    stack: `"Roboto Mono", ${MONO_TAIL}`,
    load: monoLoaders.robotoMono,
  },
  {
    label: "Inconsolata",
    family: "Inconsolata",
    stack: `"Inconsolata", ${MONO_TAIL}`,
    load: monoLoaders.inconsolata,
  },
  {
    label: "Space Mono",
    family: "Space Mono",
    stack: `"Space Mono", ${MONO_TAIL}`,
    load: monoLoaders.spaceMono,
  },
  {
    label: "Victor Mono",
    family: "Victor Mono",
    stack: `"Victor Mono", ${MONO_TAIL}`,
    load: monoLoaders.victorMono,
  },
  {
    label: "Red Hat Mono",
    family: "Red Hat Mono",
    stack: `"Red Hat Mono", ${MONO_TAIL}`,
    load: monoLoaders.redHatMono,
  },
  {
    label: "Martian Mono",
    family: "Martian Mono",
    stack: `"Martian Mono", ${MONO_TAIL}`,
    load: monoLoaders.martianMono,
  },
  { label: "Monaco", family: "Monaco", stack: `Monaco, ${MONO_TAIL}` },
  { label: "Menlo", family: "Menlo", stack: `Menlo, ${MONO_TAIL}` },
  { label: "Consolas", family: "Consolas", stack: `Consolas, ${MONO_TAIL}` },
  {
    label: "Consolas for Powerline",
    family: "Consolas for Powerline",
    stack: `"Consolas for Powerline", ${MONO_TAIL}`,
  },
  { label: "Courier New", family: "Courier New", stack: `"Courier New", ${MONO_TAIL}` },
  { label: "Andale Mono", family: "Andale Mono", stack: `"Andale Mono", ${MONO_TAIL}` },
  { label: "PT Mono", family: "PT Mono", stack: `"PT Mono", ${MONO_TAIL}` },
  { label: "Cascadia Code", family: "Cascadia Code", stack: `"Cascadia Code", ${MONO_TAIL}` },
  { label: "Cascadia Mono", family: "Cascadia Mono", stack: `"Cascadia Mono", ${MONO_TAIL}` },
  { label: "Hack", family: "Hack", stack: `Hack, ${MONO_TAIL}` },
  { label: "Iosevka", family: "Iosevka", stack: `Iosevka, ${MONO_TAIL}` },
  { label: "Anonymous Pro", family: "Anonymous Pro", stack: `"Anonymous Pro", ${MONO_TAIL}` },
  {
    label: "System (SF Mono)",
    family: "ui-monospace",
    stack: `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Roboto Mono", monospace`,
  },
];

export const UI_FONTS: FontChoice[] = [
  {
    label: "System",
    family: "-apple-system",
    stack: SANS_TAIL,
  },
  {
    label: "Inter",
    family: "Inter",
    stack: `"Inter", ${SANS_TAIL}`,
    load: proportionalLoaders.inter,
  },
  { label: "Helvetica Neue", family: "Helvetica Neue", stack: `"Helvetica Neue", ${SANS_TAIL}` },
  { label: "Helvetica", family: "Helvetica", stack: `Helvetica, ${SANS_TAIL}` },
  { label: "Arial", family: "Arial", stack: `Arial, ${SANS_TAIL}` },
  { label: "Avenir", family: "Avenir", stack: `Avenir, ${SANS_TAIL}` },
  { label: "Avenir Next", family: "Avenir Next", stack: `"Avenir Next", ${SANS_TAIL}` },
  { label: "Optima", family: "Optima", stack: `Optima, ${SANS_TAIL}` },
  { label: "Lucida Grande", family: "Lucida Grande", stack: `"Lucida Grande", ${SANS_TAIL}` },
  { label: "Verdana", family: "Verdana", stack: `Verdana, ${SANS_TAIL}` },
  { label: "Tahoma", family: "Tahoma", stack: `Tahoma, ${SANS_TAIL}` },
  { label: "Segoe UI", family: "Segoe UI", stack: `"Segoe UI", ${SANS_TAIL}` },
  { label: "Roboto", family: "Roboto", stack: `Roboto, ${SANS_TAIL}` },
];

export const TEXT_FONTS: FontChoice[] = [
  {
    label: "System",
    family: "-apple-system",
    stack: SANS_TAIL,
  },
  {
    label: "Inter",
    family: "Inter",
    stack: `"Inter", ${SANS_TAIL}`,
    load: proportionalLoaders.inter,
  },
  {
    label: "Source Serif",
    family: "Source Serif 4",
    stack: `"Source Serif 4", ${SERIF_TAIL}`,
    load: proportionalLoaders.sourceSerif4,
  },
  { label: "Georgia", family: "Georgia", stack: `Georgia, ${SERIF_TAIL}` },
  { label: "Charter", family: "Charter", stack: `Charter, ${SERIF_TAIL}` },
  { label: "Iowan Old Style", family: "Iowan Old Style", stack: `"Iowan Old Style", ${SERIF_TAIL}` },
  { label: "Palatino", family: "Palatino", stack: `Palatino, ${SERIF_TAIL}` },
  { label: "Baskerville", family: "Baskerville", stack: `Baskerville, ${SERIF_TAIL}` },
  { label: "Times New Roman", family: "Times New Roman", stack: `"Times New Roman", ${SERIF_TAIL}` },
  { label: "Times", family: "Times", stack: `Times, ${SERIF_TAIL}` },
  { label: "Helvetica Neue", family: "Helvetica Neue", stack: `"Helvetica Neue", ${SANS_TAIL}` },
  { label: "Optima", family: "Optima", stack: `Optima, ${SANS_TAIL}` },
];

const BODY_SIZE_KEY = "wiki-font-size-body";
const UI_SIZE_KEY = "wiki-font-size-ui";
const BODY_SIZE_DEFAULT = 16.5;
const UI_SIZE_DEFAULT = 13.5;

const MONO_SAMPLE = "→ const x = 0O1lIi";
const PROP_SAMPLE = "The quick brown fox";

type FontRoleId = "ui" | "text" | "mono";
type FontRole = {
  name: string;
  desc: string;
  fonts: FontChoice[];
  key: string;
  cssVar: string;
  sample: string;
};

const FONT_ROLES: Record<FontRoleId, FontRole> = {
  ui: {
    name: "Interface font",
    desc: "App chrome: sidebar, tabs, buttons, status bar, dialogs.",
    fonts: UI_FONTS,
    key: "wiki-ui-font",
    cssVar: "--font-interface",
    sample: PROP_SAMPLE,
  },
  text: {
    name: "Note font",
    desc: "Body text of rendered notes and the source editor.",
    fonts: TEXT_FONTS,
    key: "wiki-text-font",
    cssVar: "--font-text",
    sample: PROP_SAMPLE,
  },
  mono: {
    name: "Monospace font",
    desc: "Code blocks, agent transcripts, and mono UI chrome.",
    fonts: MONO_FONTS,
    key: "wiki-mono-font",
    cssVar: "--font-monospace",
    sample: MONO_SAMPLE,
  },
};

const loadedFonts = new Set<string>();
function loadFont(choice: FontChoice): Promise<void> {
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

const ALWAYS_AVAILABLE_FAMILIES = new Set(["ui-monospace", "-apple-system"]);

function isAvailable(choice: FontChoice): boolean {
  if (choice.load) return true;
  if (ALWAYS_AVAILABLE_FAMILIES.has(choice.family)) return true;
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

function pickChoice(fonts: FontChoice[], stored: string | null): FontChoice {
  return fonts.find((font) => font.label === stored) ?? fonts[0];
}

function applyFontVar(cssVar: string, choice: FontChoice) {
  document.documentElement.style.setProperty(cssVar, choice.stack);
  void loadFont(choice);
}

export function applyStoredFonts() {
  for (const role of Object.values(FONT_ROLES)) {
    applyFontVar(role.cssVar, pickChoice(role.fonts, localStorage.getItem(role.key)));
  }
  applySizes(
    storedSize(BODY_SIZE_KEY, BODY_SIZE_DEFAULT),
    storedSize(UI_SIZE_KEY, UI_SIZE_DEFAULT)
  );
}

function currentLabel(role: FontRole): string {
  return localStorage.getItem(role.key) ?? role.fonts[0].label;
}

function setFont(role: FontRole, label: string) {
  const choice = pickChoice(role.fonts, label);
  localStorage.setItem(role.key, choice.label);
  applyFontVar(role.cssVar, choice);
}

function FontPicker({
  fonts,
  current,
  sample,
  onChange,
}: {
  fonts: FontChoice[];
  current: string;
  sample: string;
  onChange: (label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [availTick, setAvailTick] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const currentChoice = useMemo(() => pickChoice(fonts, current), [fonts, current]);

  useEffect(() => {
    if (!open) return;
    void Promise.all(fonts.map(loadFont)).then(() => setAvailTick((t) => t + 1));
  }, [open, fonts]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // stop the settings modal's window keydown from closing the whole modal
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const visible = useMemo(
    () => fonts.filter((font) => font.label === current || isAvailable(font)),
    // availTick invalidates the memo after loads land
    [fonts, current, availTick]
  );

  return (
    <div className={`font-picker${open ? " is-open" : ""}`} ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        className="font-picker-trigger"
        type="button"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="font-picker-trigger-label" style={{ fontFamily: currentChoice.stack }}>
          {currentChoice.label}
        </span>
        <ChevronDown aria-hidden className="font-picker-trigger-chevron" size={14} />
      </button>
      {open ? (
        <div className="font-picker-menu" role="listbox">
          {visible.map((font) => {
            const active = font.label === current;
            return (
              <button
                aria-selected={active}
                className={`font-picker-option${active ? " is-active" : ""}`}
                key={font.label}
                role="option"
                type="button"
                onClick={() => {
                  onChange(font.label);
                  setOpen(false);
                }}
              >
                <span className="font-picker-option-label" style={{ fontFamily: font.stack }}>
                  {font.label}
                </span>
                <span
                  aria-hidden
                  className="font-picker-option-sample"
                  style={{ fontFamily: font.stack }}
                >
                  {sample}
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function FontRoleRow({ role }: { role: FontRole }) {
  const [label, setLabel] = useState(() => currentLabel(role));
  return (
    <div className="settings-row">
      <div className="settings-row-info">
        <div className="settings-row-name">{role.name}</div>
        <div className="settings-row-desc">{role.desc}</div>
      </div>
      <FontPicker
        current={label}
        fonts={role.fonts}
        sample={role.sample}
        onChange={(next) => {
          setFont(role, next);
          setLabel(next);
        }}
      />
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
          <FontRoleRow role={FONT_ROLES.ui} />
          <FontRoleRow role={FONT_ROLES.text} />
          <div className="settings-preview" style={{ fontFamily: "var(--font-text)" }}>
            The quick brown fox jumps over the lazy dog — 0123456789
          </div>
          <FontRoleRow role={FONT_ROLES.mono} />
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
