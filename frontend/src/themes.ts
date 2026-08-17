export const THEME_STORAGE_KEY = "wiki-theme";

export type ThemeFamilyId =
  | "bb"
  | "opencode"
  | "mono"
  | "gruvbox"
  | "vscode"
  | "solarized"
  | "dracula"
  | "nord"
  | "one-dark"
  | "tokyo-night"
  | "catppuccin";
export type ThemePolarity = "light" | "dark";
export type ThemeId =
  | "bb-light"
  | "bb-dark"
  | "opencode"
  | "mono-light"
  | "mono-dark"
  | "gruvbox-dark"
  | "gruvbox-light"
  | "vscode-dark-plus"
  | "solarized-dark"
  | "solarized-light"
  | "dracula"
  | "nord"
  | "one-dark"
  | "tokyo-night"
  | "catppuccin-mocha";

export interface ThemeDefinition {
  id: ThemeId;
  label: string;
  family: ThemeFamilyId;
  polarity: ThemePolarity;
  preview: readonly [string, string, string, string];
}

export const DEFAULT_THEME: ThemeId = "opencode";

export const THEMES: readonly ThemeDefinition[] = [
  {
    id: "bb-light",
    label: "bb Light",
    family: "bb",
    polarity: "light",
    preview: ["#ffffff", "#fbfbfb", "#f5f5f5", "#454545"],
  },
  {
    id: "bb-dark",
    label: "bb Dark",
    family: "bb",
    polarity: "dark",
    preview: ["#323232", "#282828", "#454545", "#d1d1d1"],
  },
  {
    id: "opencode",
    label: "OpenCode",
    family: "opencode",
    polarity: "dark",
    preview: ["#1e1e17", "#2c2c21", "#35352a", "#b18bf4"],
  },
  {
    id: "mono-light",
    label: "Mono Light",
    family: "mono",
    polarity: "light",
    preview: ["#ffffff", "#fafafa", "#dcdcdc", "#1c1c1c"],
  },
  {
    id: "mono-dark",
    label: "Mono Dark",
    family: "mono",
    polarity: "dark",
    preview: ["#111111", "#171717", "#2e2e2e", "#e8e8e8"],
  },
  {
    id: "gruvbox-dark",
    label: "Gruvbox Dark",
    family: "gruvbox",
    polarity: "dark",
    preview: ["#282828", "#3c3836", "#504945", "#83a598"],
  },
  {
    id: "gruvbox-light",
    label: "Gruvbox Light",
    family: "gruvbox",
    polarity: "light",
    preview: ["#fbf1c7", "#ebdbb2", "#d5c4a1", "#458588"],
  },
  {
    id: "vscode-dark-plus",
    label: "VS Code Dark+",
    family: "vscode",
    polarity: "dark",
    preview: ["#1f1f1f", "#181818", "#2b2b2b", "#0078d4"],
  },
  {
    id: "solarized-dark",
    label: "Solarized Dark",
    family: "solarized",
    polarity: "dark",
    preview: ["#002b36", "#073642", "#586e75", "#268bd2"],
  },
  {
    id: "solarized-light",
    label: "Solarized Light",
    family: "solarized",
    polarity: "light",
    preview: ["#fdf6e3", "#eee8d5", "#93a1a1", "#268bd2"],
  },
  {
    id: "dracula",
    label: "Dracula",
    family: "dracula",
    polarity: "dark",
    preview: ["#282a36", "#44475a", "#6272a4", "#bd93f9"],
  },
  {
    id: "nord",
    label: "Nord",
    family: "nord",
    polarity: "dark",
    preview: ["#2e3440", "#3b4252", "#4c566a", "#88c0d0"],
  },
  {
    id: "one-dark",
    label: "One Dark",
    family: "one-dark",
    polarity: "dark",
    preview: ["#282c34", "#21252b", "#3e4451", "#61afef"],
  },
  {
    id: "tokyo-night",
    label: "Tokyo Night",
    family: "tokyo-night",
    polarity: "dark",
    preview: ["#1a1b26", "#16161e", "#292e42", "#7aa2f7"],
  },
  {
    id: "catppuccin-mocha",
    label: "Catppuccin Mocha",
    family: "catppuccin",
    polarity: "dark",
    preview: ["#1e1e2e", "#181825", "#313244", "#cba6f7"],
  },
] as const;

const THEME_BY_ID = new Map(THEMES.map((theme) => [theme.id, theme]));
const FAMILY_VARIANTS: Record<ThemeFamilyId, Partial<Record<ThemePolarity, ThemeId>>> = {
  bb: { light: "bb-light", dark: "bb-dark" },
  opencode: { dark: "opencode" },
  mono: { light: "mono-light", dark: "mono-dark" },
  gruvbox: { light: "gruvbox-light", dark: "gruvbox-dark" },
  vscode: { dark: "vscode-dark-plus" },
  solarized: { light: "solarized-light", dark: "solarized-dark" },
  dracula: { dark: "dracula" },
  nord: { dark: "nord" },
  "one-dark": { dark: "one-dark" },
  "tokyo-night": { dark: "tokyo-night" },
  catppuccin: { dark: "catppuccin-mocha" },
};

export function isThemeId(value: string | null): value is ThemeId {
  return value !== null && THEME_BY_ID.has(value as ThemeId);
}

export function normalizeTheme(value: string | null): ThemeId {
  if (value === "light") return "mono-light";
  if (value === "dark") return "mono-dark";
  return isThemeId(value) ? value : DEFAULT_THEME;
}

export function getTheme(themeId: ThemeId): ThemeDefinition {
  return THEME_BY_ID.get(themeId) ?? THEME_BY_ID.get(DEFAULT_THEME)!;
}

export function getStoredTheme(): ThemeId {
  return normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY));
}

export function setStoredTheme(themeId: ThemeId) {
  localStorage.setItem(THEME_STORAGE_KEY, themeId);
}

export function applyTheme(themeId: ThemeId) {
  document.documentElement.dataset.theme = themeId;
  setStoredTheme(themeId);
}

export function applyStoredTheme(): ThemeId {
  const themeId = getStoredTheme();
  applyTheme(themeId);
  return themeId;
}

export function isDarkTheme(themeId: ThemeId) {
  return getTheme(themeId).polarity === "dark";
}

export function toggleThemePolarity(themeId: ThemeId): ThemeId {
  const theme = getTheme(themeId);
  const family = FAMILY_VARIANTS[theme.family];
  if (theme.polarity === "dark") return family.light ?? "mono-light";
  return family.dark ?? "mono-dark";
}
