// @vitest-environment jsdom
import { expect, test } from "vitest";

import { groupEdges } from "../src/workgraph-panel";

test("legacy workgraph edges without active keep the historical style", () => {
  const [edge] = groupEdges([
    {
      kind: "spawn",
      from: "orch:wiki",
      to: "WIKI-170",
      created_at: "2026-07-29T12:00:00Z",
    },
  ]);

  expect(edge.active).toBe(false);
});
