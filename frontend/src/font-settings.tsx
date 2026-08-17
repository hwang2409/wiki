import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { INSTALLED_FONT_FACE_REGISTERED_EVENT, type FontChoice } from "./font-enumeration";
import {
  clampWeight,
  detectFontWeights,
  handleInstalledFontFaceRegistered,
  isAvailable,
  loadFontFaces,
  pickChoice,
  preferredWeight,
  PROP_SAMPLE,
  setFontWeight,
  storedWeight,
  weightLabel,
} from "./settings";

export interface FontSettingsProps {
  fonts: FontChoice[];
  initialLabel: string;
  initialWeight: number;
  onFontChange: (label: string) => void;
  onWeightChange: (weight: number) => void;
}

function FontPicker({
  fonts,
  current,
  sample,
  weight,
  onChange,
}: {
  fonts: FontChoice[];
  current: string;
  sample: string;
  weight: number;
  onChange: (label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [availTick, setAvailTick] = useState(0);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const filterRef = useRef<HTMLInputElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const wasOpenRef = useRef(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const currentChoice = useMemo(() => pickChoice(fonts, current), [fonts, current]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    setAvailTick((t) => t + 1);
  }, [open, fonts]);

  useEffect(() => {
    if (!open) return;
    void Promise.all(fonts.map(loadFontFaces));
  }, [open, fonts]);

  useEffect(() => {
    if (!open) return;
    // Focus the filter on open so power users can type-narrow immediately.
    filterRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
    };
  }, [open]);

  useEffect(() => {
    if (wasOpenRef.current && !open) triggerRef.current?.focus();
    wasOpenRef.current = open;
  }, [open]);

  const available = useMemo(
    () => fonts.filter((font) => font.label === current || isAvailable(font)),
    // availTick invalidates the memo after loads land
    [fonts, current, availTick]
  );
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return available;
    return available.filter((font) => font.label.toLowerCase().includes(needle));
  }, [available, query]);

  useEffect(() => {
    if (!open) return;
    const selectedIndex = visible.findIndex((font) => font.label === current);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
  }, [current, open, visible]);

  function focusOption(index: number) {
    if (visible.length === 0) return;
    const nextIndex = Math.max(0, Math.min(index, visible.length - 1));
    setActiveIndex(nextIndex);
    optionRefs.current[nextIndex]?.focus();
  }

  function handlePickerNavigation(event: React.KeyboardEvent<HTMLElement>) {
    if (visible.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusOption(activeIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusOption(activeIndex - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusOption(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusOption(visible.length - 1);
    }
  }

  return (
    <div
      className={`font-picker${open ? " is-open" : ""}`}
      ref={rootRef}
      onKeyDown={(event) => {
        if (!open || event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }}
    >
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        className="font-picker-trigger"
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
      >
        <span
          className="font-picker-trigger-label"
          style={{ fontFamily: currentChoice.stack, fontWeight: weight }}
        >
          {currentChoice.label}
        </span>
        <ChevronDown aria-hidden className="font-picker-trigger-chevron" size={14} />
      </button>
      {open ? (
        <div className="font-picker-menu">
          <input
            aria-label="Filter fonts"
            className="font-picker-filter"
            placeholder="Filter fonts"
            ref={filterRef}
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              handlePickerNavigation(event);
              if (event.key === "Enter" && visible.length > 0) {
                event.preventDefault();
                onChange(visible[0].label);
                setOpen(false);
              }
            }}
          />
          <div
            aria-label="Available fonts"
            className="font-picker-options"
            role="listbox"
            onKeyDown={handlePickerNavigation}
          >
            {visible.length === 0 ? (
              <div className="font-picker-empty">No fonts match “{query}”.</div>
            ) : null}
            {visible.map((font, index) => {
              const active = font.label === current;
              return (
                <button
                  aria-selected={active}
                  className={`font-picker-option${active ? " is-active" : ""}`}
                  key={font.label}
                  ref={(element) => { optionRefs.current[index] = element; }}
                  role="option"
                  tabIndex={index === activeIndex ? 0 : -1}
                  type="button"
                  onClick={() => {
                    onChange(font.label);
                    setOpen(false);
                  }}
                >
                  <span
                    className="font-picker-option-label"
                    style={{ fontFamily: font.stack, fontWeight: weight }}
                  >
                    {font.label}
                  </span>
                  <span
                    aria-hidden
                    className="font-picker-option-sample"
                    style={{ fontFamily: font.stack, fontWeight: weight }}
                  >
                    {sample}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function FontRow({
  fonts,
  label,
  weight,
  onFontChange,
  onWeightChange,
}: {
  fonts: FontChoice[];
  label: string;
  weight: number;
  onFontChange: (label: string) => void;
  onWeightChange: (weight: number) => void;
}) {
  const [weights, setWeights] = useState<number[]>([]);
  const [fontFaceRevision, setFontFaceRevision] = useState(0);
  const [weightText, setWeightText] = useState(() => String(storedWeight() ?? 400));
  const choice = useMemo(() => pickChoice(fonts, label), [fonts, label]);

  useEffect(() => {
    const onFaceRegistered = (event: Event) => {
      const family = handleInstalledFontFaceRegistered(event);
      if (family !== choice.family) return;
      setFontFaceRevision((revision) => revision + 1);
    };
    window.addEventListener(INSTALLED_FONT_FACE_REGISTERED_EVENT, onFaceRegistered);
    return () => window.removeEventListener(INSTALLED_FONT_FACE_REGISTERED_EVENT, onFaceRegistered);
  }, [choice.family]);

  useEffect(() => {
    let cancelled = false;
    void loadFontFaces(choice).then(() => {
      if (cancelled) return;
      const nextWeights = detectFontWeights(choice.family);
      // A saved weight is respected as-is, even off the detected stops —
      // arbitrary values are the point (variable fonts). Only the unset case
      // adopts the face's preferred default.
      const savedWeight = storedWeight();
      const nextWeight = savedWeight ?? preferredWeight(nextWeights);
      if (savedWeight === null && nextWeight !== 400) {
        setFontWeight(nextWeight);
      }
      setWeights(nextWeights);
      onWeightChange(nextWeight);
      setWeightText(String(nextWeight));
    });
    return () => {
      cancelled = true;
    };
  }, [choice, fontFaceRevision, onWeightChange]);

  const applyWeight = (next: number) => {
    setFontWeight(next);
    onWeightChange(next);
  };

  return (
    <div className="settings-row">
      <div className="settings-row-info">
        <div className="settings-row-name">Font</div>
        <div className="settings-row-desc">
          One family for every surface — prose, chat, code, diffs, chrome, dashboard.
        </div>
      </div>
      <div className="font-setting-controls">
        <FontPicker
          current={label}
          fonts={fonts}
          sample={PROP_SAMPLE}
          weight={weight}
          onChange={(next) => {
            onFontChange(next);
            setWeights([]);
          }}
        />
        <input
          aria-label="Font weight (1–1000)"
          className="font-weight-input"
          inputMode="numeric"
          max={1000}
          min={1}
          step={1}
          title="Font weight, any value from 1 to 1000"
          type="number"
          value={weightText}
          onBlur={() => {
            const parsed = Number(weightText);
            const next = Number.isFinite(parsed) && weightText.trim() !== "" ? clampWeight(parsed) : weight;
            applyWeight(next);
            setWeightText(String(next));
          }}
          onChange={(event) => {
            const raw = event.target.value;
            setWeightText(raw);
            const parsed = Number(raw);
            if (Number.isFinite(parsed) && parsed >= 1 && parsed <= 1000) {
              applyWeight(clampWeight(parsed));
            }
          }}
        />
        {weights.length > 1 ? (
          <div aria-label="Detected font weights" className="font-weight-stops" role="group">
            {weights.map((option) => (
              <button
                key={option}
                className={`font-weight-stop${option === weight ? " is-active" : ""}`}
                title={weightLabel(option)}
                type="button"
                onClick={() => {
                  applyWeight(option);
                  setWeightText(String(option));
                }}
              >
                {option}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}


export function FontSettings({
  fonts,
  initialLabel,
  initialWeight,
  onFontChange,
  onWeightChange,
}: FontSettingsProps) {
  const [label, setLabel] = useState(initialLabel);
  const [weight, setWeight] = useState(initialWeight);
  const handleFontChange = useCallback((next: string) => {
    setLabel(next);
    onFontChange(next);
  }, [onFontChange]);
  const handleWeightChange = useCallback((next: number) => {
    setWeight(next);
    onWeightChange(next);
  }, [onWeightChange]);

  return (
    <FontRow
      fonts={fonts}
      label={label}
      weight={weight}
      onFontChange={handleFontChange}
      onWeightChange={handleWeightChange}
    />
  );
}
