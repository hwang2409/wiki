// @vitest-environment jsdom
//
// WIKI-276 S1: the ticket row must render every PR (primary + older) with
// its own state chip, and collapse the non-primary PRs behind a
// disclosure. Reviewer's rule: assert the actual DOM, not the payload.

import { describe, expect, test, beforeEach } from "vitest";

import {
  prLabel,
  renderTicketRowHtml,
} from "../../backend/app/dashboard_static/ticket-row.mjs";

function inject(html: string): HTMLTableRowElement {
  const table = document.createElement("table");
  const tbody = document.createElement("tbody");
  tbody.innerHTML = html;
  table.appendChild(tbody);
  document.body.appendChild(table);
  const row = tbody.querySelector("tr");
  if (!row) throw new Error("no <tr> produced");
  return row as HTMLTableRowElement;
}

const PR_NEW = "https://github.com/phoebe-health/phoebe/pull/200";
const PR_OLD = "https://github.com/phoebe-health/phoebe/pull/100";
const PR_OLDER = "https://github.com/phoebe-health/phoebe/pull/50";

function ticketFixture(overrides: Record<string, unknown> = {}) {
  return {
    ticket: "PHO-42",
    base_ticket: "PHO-42",
    description: "adjust the widget",
    pr: PR_NEW,
    repo: "phoebe-health/phoebe",
    enriched: true,
    status: "passing",
    detail: null,
    date: "2026-08-11T14:00:00+00:00",
    live: true,
    role: "implement",
    kind: "cc",
    prs: [
      { pr: PR_NEW, status: "passing", detail: null, stale: false, enriched: true, date: "2026-08-11T14:00:00+00:00" },
      { pr: PR_OLD, status: "merged", detail: null, stale: false, enriched: true, date: "2026-08-05T00:00:00+00:00" },
    ],
    workers_live: [],
    review_round: null,
    next_action: null,
    ...overrides,
  };
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("prLabel", () => {
  test("parses github pull URLs", () => {
    expect(prLabel(PR_NEW)).toEqual({ num: "#200", repo: "phoebe-health/phoebe" });
  });
  test("returns null for empty", () => {
    expect(prLabel("")).toBeNull();
    expect(prLabel(null)).toBeNull();
  });
});

describe("renderTicketRowHtml — per-PR chips + collapse", () => {
  test("primary PR gets its own state chip in the DOM", () => {
    const row = inject(renderTicketRowHtml(ticketFixture(), []));
    const chip = row.querySelector(".pr-primary-chip .pill");
    expect(chip, "primary PR must carry its own chip").not.toBeNull();
    expect(chip!.textContent).toBe("passing");
    expect(chip!.classList.contains("is-passing")).toBe(true);
  });

  test("older PR is hidden behind a collapsed <details> disclosure", () => {
    const row = inject(renderTicketRowHtml(ticketFixture(), []));
    const details = row.querySelector("details.pr-older") as HTMLDetailsElement | null;
    expect(details, "older-PRs disclosure element must exist").not.toBeNull();
    expect(details!.open).toBe(false);
    const summary = details!.querySelector("summary");
    expect(summary!.textContent).toBe("1 older PR");
    // Even when collapsed, the older PR + its chip are in the DOM.
    const olderLink = details!.querySelector('a[href="' + PR_OLD + '"]');
    expect(olderLink, "older PR link must render inside the disclosure").not.toBeNull();
    const olderChip = details!.querySelector(".pr-chip .pill");
    expect(olderChip, "older PR must carry its own state chip").not.toBeNull();
    expect(olderChip!.textContent).toBe("merged");
    expect(olderChip!.classList.contains("is-merged")).toBe(true);
    // A collapsed <details> without `open` renders its children as
    // hidden by user agent — but the DOM node exists, so opening it
    // reveals the chip that's already there.
    details!.open = true;
    // After opening the same chip is available; nothing is regenerated.
    const openedChip = row.querySelector("details.pr-older[open] .pr-chip .pill");
    expect(openedChip).not.toBeNull();
    expect(openedChip!.textContent).toBe("merged");
  });

  test("multiple older PRs pluralize and each carries its chip", () => {
    const ticket = ticketFixture({
      prs: [
        { pr: PR_NEW, status: "passing", detail: null, stale: false, enriched: true, date: "2026-08-11T14:00:00+00:00" },
        { pr: PR_OLD, status: "merged", detail: null, stale: false, enriched: true, date: "2026-08-05T00:00:00+00:00" },
        { pr: PR_OLDER, status: "closed", detail: null, stale: false, enriched: true, date: "2026-07-30T00:00:00+00:00" },
      ],
    });
    const row = inject(renderTicketRowHtml(ticket, []));
    const details = row.querySelector("details.pr-older") as HTMLDetailsElement;
    expect(details.querySelector("summary")!.textContent).toBe("2 older PRs");
    const items = details.querySelectorAll(".pr-older-item");
    expect(items).toHaveLength(2);
    const statuses = Array.from(items).map((el) => {
      const pill = el.querySelector(".pr-chip .pill");
      return pill?.textContent;
    });
    expect(statuses).toEqual(["merged", "closed"]);
  });

  test("no disclosure when only the primary PR exists", () => {
    const ticket = ticketFixture({
      prs: [
        { pr: PR_NEW, status: "passing", detail: null, stale: false, enriched: true, date: "2026-08-11T14:00:00+00:00" },
      ],
    });
    const row = inject(renderTicketRowHtml(ticket, []));
    expect(row.querySelector("details.pr-older")).toBeNull();
    // Primary chip still present.
    expect(row.querySelector(".pr-primary-chip .pill")!.textContent).toBe("passing");
  });

  test("PR chip carries the underlying state class (failing/prod/etc.)", () => {
    const ticket = ticketFixture({
      pr: PR_NEW,
      status: "failing",
      prs: [
        { pr: PR_NEW, status: "failing", detail: "pytest", stale: false, enriched: true, date: "2026-08-11T14:00:00+00:00" },
        { pr: PR_OLD, status: "prod", detail: null, stale: false, enriched: true, date: "2026-08-05T00:00:00+00:00" },
      ],
    });
    const row = inject(renderTicketRowHtml(ticket, []));
    const primary = row.querySelector(".pr-primary-chip .pill")!;
    expect(primary.classList.contains("is-failing")).toBe(true);
    const older = row.querySelector("details.pr-older .pr-chip .pill")!;
    expect(older.classList.contains("is-prod")).toBe(true);
  });

  test("no PR at all → em-dash in cell, no chip, no disclosure", () => {
    const ticket = ticketFixture({ pr: null, prs: [] });
    const row = inject(renderTicketRowHtml(ticket, []));
    const cell = row.querySelector("td.pr-cell");
    expect(cell?.classList.contains("empty")).toBe(true);
    expect(cell!.textContent).toBe("—");
    expect(row.querySelector(".pr-primary-chip")).toBeNull();
    expect(row.querySelector("details.pr-older")).toBeNull();
  });
});
