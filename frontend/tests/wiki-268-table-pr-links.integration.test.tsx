// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { getGhPreview } from "../src/api";
import { ObsidianMarkdown } from "../src/markdown";

vi.mock("../src/api", () => ({
  getGhPreview: vi.fn(() => new Promise(() => {})),
}));

const getGhPreviewMock = vi.mocked(getGhPreview);

const PR_URL = "https://github.com/phoebehq/phoebe/pull/13678";
const COMMIT_URL = "https://github.com/henrywang/wiki/commit/dd3898bcafe1234567890abcdef123456789012";

function renderMarkdown(content: string) {
  return render(
    <ObsidianMarkdown
      content={content}
      notes={[]}
      onOpenNote={() => {}}
    />,
  );
}

afterEach(() => {
  cleanup();
  getGhPreviewMock.mockClear();
});

describe("WIKI-268 GitHub link rendering by context", () => {
  test("PR URL in a table cell renders the compact inline form, not the card", () => {
    const table = `| ticket | pr |\n| --- | --- |\n| WIKI-268 | ${PR_URL} |\n`;
    const { container } = renderMarkdown(table);

    const inline = container.querySelector<HTMLAnchorElement>(`td a.gh-preview-inline[href="${PR_URL}"]`);
    expect(inline).not.toBeNull();
    expect(inline?.classList.contains("is-pr")).toBe(true);
    expect(inline?.textContent).toContain("phoebe#13678");
    expect(container.querySelector(`a.gh-preview-card[href="${PR_URL}"]`)).toBeNull();
    // Compact form must not fetch preview metadata — that would flood requests
    // when a fleet-status table lands with dozens of PR links.
    expect(getGhPreviewMock).not.toHaveBeenCalled();
  });

  test("PR URL in a paragraph still renders the full card", () => {
    const { container } = renderMarkdown(`See [pr](${PR_URL}) for context.`);

    // GhPreviewCard renders a plain fallback anchor while the metadata fetch
    // is pending — assert the card *path* was taken by confirming the fetch
    // was triggered (inline path never calls getGhPreview).
    expect(getGhPreviewMock).toHaveBeenCalledWith(PR_URL, undefined);
    expect(container.querySelector(`a.gh-preview-inline[href="${PR_URL}"]`)).toBeNull();
  });

  test("commit URL in a table cell renders the short-sha compact form", () => {
    const table = `| commit |\n| --- |\n| ${COMMIT_URL} |\n`;
    const { container } = renderMarkdown(table);

    const inline = container.querySelector<HTMLAnchorElement>(`td a.gh-preview-inline[href="${COMMIT_URL}"]`);
    expect(inline).not.toBeNull();
    expect(inline?.classList.contains("is-commit")).toBe(true);
    expect(inline?.textContent).toContain("wiki@dd3898b");
  });
});
