import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { ObsidianMarkdown } from "../src/markdown";

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
});

describe("WIKI-255 math parsing", () => {
  test("does not parse currency prose as inline math", () => {
    const { container } = renderMarkdown("sellers earned $5 and $12 today, so profit is $-3");

    expect(container.querySelectorAll(".katex")).toHaveLength(0);
    expect(container.textContent).toContain("sellers earned $5 and $12 today, so profit is $-3");
  });

  test("still parses escaped inline math", () => {
    const { container } = renderMarkdown(String.raw`The area is \( \pi r^2 \)`);

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(0);
  });

  test("still parses block math", () => {
    const { container } = renderMarkdown("$$\nE = mc^2\n$$");

    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
  });
});
