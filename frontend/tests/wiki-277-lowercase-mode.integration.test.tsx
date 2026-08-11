import { afterEach, expect, test } from "vitest";
import {
  LOWERCASE_ROOT_CLASS,
  LOWERCASE_STORAGE_KEY,
  applyLowercase,
  applyStoredLowercase,
  getStoredLowercase,
} from "../src/lowercase-mode";

afterEach(() => {
  localStorage.removeItem(LOWERCASE_STORAGE_KEY);
  document.documentElement.classList.remove(LOWERCASE_ROOT_CLASS);
});

test("default is off — no class, no stored value", () => {
  expect(getStoredLowercase()).toBe(false);
  expect(applyStoredLowercase()).toBe(false);
  expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(false);
});

test("enabling adds the root class and persists to localStorage", () => {
  applyLowercase(true);
  expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(true);
  expect(localStorage.getItem(LOWERCASE_STORAGE_KEY)).toBe("true");
  expect(getStoredLowercase()).toBe(true);
});

test("disabling removes the root class and clears storage", () => {
  applyLowercase(true);
  applyLowercase(false);
  expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(false);
  expect(localStorage.getItem(LOWERCASE_STORAGE_KEY)).toBeNull();
  expect(getStoredLowercase()).toBe(false);
});

test("setting survives a simulated reload via applyStoredLowercase", () => {
  applyLowercase(true);
  document.documentElement.classList.remove(LOWERCASE_ROOT_CLASS);
  expect(applyStoredLowercase()).toBe(true);
  expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(true);
});
