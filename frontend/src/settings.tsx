import { useEffect, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { THEMES, type ThemeId } from "./themes";

export type MonoFontChoice = { label: string; stack: string };

export const MONO_FONTS: MonoFontChoice[] = [
  {
    label: "JetBrains Mono",
    stack: '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Geist Mono",
    stack: '"Geist Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Fira Code",
    stack: '"Fira Code", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "IBM Plex Mono",
    stack: '"IBM Plex Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Source Code Pro",
    stack: '"Source Code Pro", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Roboto Mono",
    stack: '"Roboto Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Inconsolata",
    stack: '"Inconsolata", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Space Mono",
    stack: '"Space Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Victor Mono",
    stack: '"Victor Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Red Hat Mono",
    stack: '"Red Hat Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Martian Mono",
    stack: '"Martian Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  {
    label: "Menlo",
    stack: 'Menlo, ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace',
  },
  {
    label: "System (SF Mono)",
    stack: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Roboto Mono", monospace',
  },
];

const MONO_FONT_KEY = "wiki-mono-font";
const BODY_SIZE_KEY = "wiki-font-size-body";
const UI_SIZE_KEY = "wiki-font-size-ui";
const BODY_SIZE_DEFAULT = 16.5;
const UI_SIZE_DEFAULT = 13.5;

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
  const choice = MONO_FONTS.find((font) => font.label === stored);
  if (choice) {
    document.documentElement.style.setProperty("--font-monospace", choice.stack);
  }
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
            <div className="settings-select">
              <select
                value={monoFont}
                onChange={(event) => {
                  setMonoFont(event.target.value);
                  setMonoFontState(event.target.value);
                }}
              >
                {MONO_FONTS.map((font) => (
                  <option key={font.label} style={{ fontFamily: font.stack }} value={font.label}>
                    {font.label}
                  </option>
                ))}
              </select>
              <span aria-hidden className="settings-select-chevron">
                <ChevronDown size={14} />
              </span>
            </div>
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
