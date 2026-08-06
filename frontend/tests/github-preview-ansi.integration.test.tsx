// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { renderAnsiWithGitHubLinks } from "../src/github-preview";

vi.mock("../src/api", () => ({
  getGhPreview: vi.fn(() => new Promise(() => {})),
}));

afterEach(() => {
  cleanup();
});

const URL = "https://github.com/hwang2409/wiki/pull/1";

test("SGR style survives across a GitHub URL boundary", () => {
  const text = `\x1b[31mred text ${URL} still red\x1b[0m`;
  const { container } = render(<>{renderAnsiWithGitHubLinks(text)}</>);

  const styled = container.querySelectorAll("span.ansi-fg-1");
  const styledTexts = Array.from(styled).map((el) => el.textContent);
  expect(styledTexts).toContain("red text ");
  expect(styledTexts).toContain(" still red");
  expect(container.querySelector(`a.external-link[href="${URL}"]`)).not.toBeNull();
  expect(container.querySelector(`a.gh-preview-card[href="${URL}"]`)).toBeNull();
  expect(container.textContent ?? "").not.toContain("\x1b");
});

test("OSC-8 hyperlink escape is not leaked as visible bytes", () => {
  const text = `before \x1b]8;;${URL}\x1b\\click here\x1b]8;;\x1b\\ after`;
  const { container } = render(<>{renderAnsiWithGitHubLinks(text)}</>);

  const visible = container.textContent ?? "";
  expect(visible).not.toContain("\x1b");
  expect(visible).not.toContain("]8;");
  expect(visible).toContain("click here");
  expect(visible).toContain("before ");
  expect(visible).toContain(" after");
});

test("WIKI-252: URL renders as plain external-link anchor, not a preview card", () => {
  const text = `plain ${URL} plain`;
  const { container } = render(<>{renderAnsiWithGitHubLinks(text)}</>);
  const anchor = container.querySelector<HTMLAnchorElement>(`a.external-link[href="${URL}"]`);
  expect(anchor).not.toBeNull();
  expect(anchor?.getAttribute("target")).toBe("_blank");
  expect(anchor?.textContent).toBe(URL);
  expect(container.querySelector(`a.gh-preview-card[href="${URL}"]`)).toBeNull();
});
