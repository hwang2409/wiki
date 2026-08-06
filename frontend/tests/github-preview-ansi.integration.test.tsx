// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { getGhPreview } from "../src/api";
import { renderAnsiWithGitHubLinks } from "../src/github-preview";

vi.mock("../src/api", () => ({
  getGhPreview: vi.fn(() => new Promise(() => {})),
}));

const getGhPreviewMock = vi.mocked(getGhPreview);

afterEach(() => {
  cleanup();
  getGhPreviewMock.mockClear();
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

test("WIKI-252: tool-output renderer emits plain anchor with no metadata fetch", () => {
  // Mutation sensitivity: <GhPreviewCard> also renders the exact plain
  // fallback anchor while its metadata fetch is pending, so checking the
  // anchor alone would pass under BOTH the old (card) and new (no card)
  // renderers when the mocked fetch never resolves. Anchor the assertion
  // to what only the plain path does: it never calls getGhPreview.
  const text = `plain ${URL} plain`;
  const { container } = render(<>{renderAnsiWithGitHubLinks(text)}</>);
  const anchor = container.querySelector<HTMLAnchorElement>(`a.external-link[href="${URL}"]`);
  expect(anchor).not.toBeNull();
  expect(anchor?.getAttribute("target")).toBe("_blank");
  expect(anchor?.getAttribute("rel") ?? "").toMatch(/\bnoopener\b/);
  expect(anchor?.getAttribute("rel") ?? "").toMatch(/\bnoreferrer\b/);
  expect(anchor?.textContent).toBe(URL);
  expect(container.querySelector(`a.gh-preview-card[href="${URL}"]`)).toBeNull();
  expect(getGhPreviewMock).not.toHaveBeenCalled();
});
