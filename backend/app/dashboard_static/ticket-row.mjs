// WIKI-276 ticket-row rendering (S1).
//
// Pure functions that build the HTML for one row of the task table.
// Kept in its own ES module so a jsdom unit test can exercise the
// per-PR chip + collapsible-older-PRs behavior without evaling the
// whole /dashboard page. The browser loads this module via
// <script type="module"> and calls `renderTicketRowHtml` from the
// inline dashboard script; the test imports it directly.

export function escape(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function prLabel(url) {
  if (!url) return null;
  const m = url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
  if (!m) return { num: url, repo: null };
  return { num: "#" + m[2], repo: m[1] };
}

export function agePhrase(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return seconds + "s";
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m";
  const d = Math.floor(h / 24);
  return d + "d " + (h % 24) + "h";
}

export function ageFromIso(iso, nowMs) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (isNaN(t)) return null;
  const now = typeof nowMs === "number" ? nowMs : Date.now();
  return (now - t) / 1000;
}

export function pill(label, cls) {
  return '<span class="pill is-' + cls + '">' + escape(label) + '</span>';
}

export function nextActionClass(hint) {
  if (!hint) return "";
  if (/CI red|blocked|stalled|stall|unrouted|waiting/i.test(hint)) return "is-urgent";
  if (/ready to merge|awaiting Henry/i.test(hint)) return "is-ready";
  if (/review/i.test(hint)) return "is-review";
  return "";
}

export function ticketBaseLive(ticket, workers) {
  return (workers || []).filter((w) => (w.base_ticket || w.ticket) === ticket);
}

// Chip that carries a PR's own state (open/merged/closed + pass/fail/pending)
// so a ticket row displays the actual state of every PR it has held,
// not just a bare link (S1).
function prChipHtml(entry) {
  const status = entry.status || "unknown";
  const cls = status.replace(/[^a-z0-9-]/gi, "-");
  const detail = entry.detail
    ? ' <span class="pr-chip-detail">' + escape(entry.detail) + '</span>'
    : "";
  const stale = entry.stale ? ' <span class="pr-stale">stale</span>' : "";
  return '<span class="pr-chip">' + pill(status, cls) + detail + stale + '</span>';
}

function prLinkHtml(url, includeRepo) {
  const lbl = prLabel(url);
  if (!lbl) return escape(url || "");
  const repo = includeRepo && lbl.repo
    ? '<br><span class="pr-repo">' + escape(lbl.repo) + '</span>'
    : "";
  return '<a href="' + escape(url) + '" target="_blank" rel="noopener">' +
    '<span class="pr-num">' + escape(lbl.num) + '</span>' + repo + '</a>';
}

// Build a full <tr> for one ticket row. The output includes:
//   - the primary PR link + its own state chip (S1);
//   - a <details> disclosure listing every non-primary PR, each carrying
//     its own state chip, collapsed by default with a
//     "N older PR" summary (S1).
export function renderTicketRowHtml(ticket, workers, options) {
  const opts = options || {};
  const nowMs = typeof opts.nowMs === "number" ? opts.nowMs : Date.now();
  const t = ticket || {};
  const primaryPr = prLabel(t.pr);
  const statusCls = (t.status || "unknown").replace(/[^a-z0-9-]/gi, "-");
  const age = agePhrase(ageFromIso(t.date, nowMs));
  const linkedWorkers = ticketBaseLive(t.ticket, workers);
  const linkPill = linkedWorkers.length > 0
    ? '<a class="worker-link" data-target="worker-' + escape(linkedWorkers[0].ticket) +
      '">' + linkedWorkers.length + ' live worker' +
      (linkedWorkers.length === 1 ? '' : 's') + '</a>'
    : '';
  const detail = t.detail ? '<span class="detail">' + escape(t.detail) + '</span>' : '';
  const roundPill = t.review_round ? ' ' + pill("r" + t.review_round, "round") : '';
  const hint = t.next_action
    ? ' <span class="next-action ' + nextActionClass(t.next_action) + '">' +
      escape(t.next_action) + '</span>'
    : '';
  const prList = Array.isArray(t.prs) ? t.prs : [];
  const primaryEntry = prList.find((entry) => entry.pr === t.pr) || null;
  const primaryChip = primaryEntry ? prChipHtml(primaryEntry) : "";
  const olderEntries = prList.filter((entry) => entry.pr && entry.pr !== t.pr);
  const olderHtml = olderEntries.length > 0
    ? '<details class="pr-older" data-count="' + olderEntries.length + '">' +
        '<summary class="pr-older-summary">' +
          escape(olderEntries.length + ' older PR' + (olderEntries.length === 1 ? '' : 's')) +
        '</summary>' +
        '<ul class="pr-older-list">' + olderEntries.map((entry) => {
          return '<li class="pr-older-item">' +
            prLinkHtml(entry.pr, true) + ' ' + prChipHtml(entry) +
          '</li>';
        }).join("") + '</ul>' +
      '</details>'
    : "";
  const primaryStale = primaryEntry && primaryEntry.stale
    ? '<span class="pr-stale">stale</span>'
    : "";
  const prCell = primaryPr
    ? prLinkHtml(t.pr, true) + primaryStale +
      (primaryChip ? '<div class="pr-primary-chip">' + primaryChip + '</div>' : "") +
      olderHtml
    : olderHtml || '—';
  return (
    '<tr id="ticket-' + escape(t.ticket) + '">' +
      '<td class="ticket-id">' + escape(t.ticket) + '</td>' +
      '<td class="desc-cell">' +
        escape(t.description || "—") +
        detail +
        (linkPill ? '<br>' + linkPill : '') +
      '</td>' +
      '<td class="pr-cell' + (primaryPr || olderEntries.length > 0 ? '' : ' empty') + '">' +
        prCell +
      '</td>' +
      '<td>' + pill(t.status || "unknown", statusCls) + roundPill + hint + '</td>' +
      '<td class="age-cell">' + escape(age) + '</td>' +
    '</tr>'
  );
}
