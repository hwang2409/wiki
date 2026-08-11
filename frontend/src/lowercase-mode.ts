export const LOWERCASE_STORAGE_KEY = "wiki-lowercase-mode";
export const LOWERCASE_ROOT_CLASS = "lowercase-mode";

export function getStoredLowercase(): boolean {
  return localStorage.getItem(LOWERCASE_STORAGE_KEY) === "true";
}

export function applyLowercase(enabled: boolean): void {
  document.documentElement.classList.toggle(LOWERCASE_ROOT_CLASS, enabled);
  if (enabled) localStorage.setItem(LOWERCASE_STORAGE_KEY, "true");
  else localStorage.removeItem(LOWERCASE_STORAGE_KEY);
}

export function applyStoredLowercase(): boolean {
  const enabled = getStoredLowercase();
  document.documentElement.classList.toggle(LOWERCASE_ROOT_CLASS, enabled);
  return enabled;
}
