---
type: til
tags: [wiki-app, frontend, rca]
created: 2026-08-27
updated: 2026-09-24
---

# Transcript row cache: display-space vs session-space index mismatch


RCA for "sent message doesn't render until I tab out/in of the pane" (Henry report 2026-08-27; fix PR #298).

## Defect class

An incremental cache that accepts a "changed from index N" hint is only sound if the hint indexes the SAME array the cache diffs. `SessionTab.displayEvents` splices synthetic user rows (optimistic pending sends id 3M+, composer fallbacks id 2M+, markers id 1M+) into `session.events` at timestamp/floor positions — so display indices shift relative to session indices. `eventRowsIncremental` took `session.eventsChangedFrom` (session-space) as its rebuild start for the display-space array. A synthetic row spliced below the hint was skipped in the partial rebuild (stale prefix kept, boundary row duplicated) and stayed missing on every later poll; only a pane remount (fresh component cache) repaired it. Introduced with the optimistic-send feature (#282, 2026-08-20).

## Fix shape (PR #298)

- `eventRowsIncremental` computes the change point via full prefix scan by object identity; hint parameter removed. NOTE: `firstChangedRef`'s append fast path (checks only the last prefix element) is also unsound for spliced input — kept only for `buildVirtualLayoutIncremental`, whose rows changes are suffix-only by construction.
- `eventsChangedFrom` plumbing deleted from transcript-merge/store (row cache was sole consumer).
- `stabilizeSyntheticEvents` reuses prior synthetic event objects across recomputes when flat fields are unchanged — keeps the identity diff tight and preserves `prev.row.event === next.row.event` memoization.

## Reusable lessons

- 2026-09-24 recurrence on `feebonator`: the live API had user event 585, but the open pane omitted it while showing later events. Switching panes made it appear. `pendingTimelineRef` kept acknowledged sends and `mergePendingEvents` moved their real echoes back to the send floor. A regression test proves that this changes provider order; a remount clears the old history. The fix leaves acknowledged echoes at their provider position. The exact prior viewport state was not captured, so this is the confirmed pane defect path, not proof that every missing row had this cause.
- At 22:09:19Z a fleet notice and Henry's message arrived 226 ms apart. Claude echoed them as one newline-joined user event 554. The old exact/hook matcher recorded neither send. The fix matches the complete joined text, records one receipt per pending ID, and correlates both receipts with the one visible event. Unmatched provider echoes still do not create receipts.

- Remount-fixes-it symptoms in a polled view = component-level incremental cache staleness, not store staleness. Probe: remount a second view over the same store at failure time.
- Identity-diff invariant: safe only while nothing mutates events in place. transcript-merge always allocates new objects on change — preserve that.
- A seeded interleaving fuzzer over the real component (fake server: events + composer_messages + queue; actions: send/queue/ack/echo/respond/poll/suffix-resend) found in minutes what hand-tracing missed. Harness: `frontend/tests/sent-message-render-fuzz.integration.test.tsx`.
