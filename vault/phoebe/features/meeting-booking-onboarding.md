---
type: reference
tags: [phoebe]
created: 2026-07-16
updated: 2026-07-16
---

# Meeting booking onboarding

Core owns kickoff booking state; the customer onboarding surface should call Core through the Phoebe API rather than create a second Calendly pipeline.

- Source: PHO-12880, current `origin/main`, and `.agents/plans/meeting-booking-onboarding.md` in the Phoebe worktree (2026-07-16).
- Core resolves a Phoebe organization to its account through `accounts.phoebeOrganizationId`; Phoebe does not persist a Core account UUID.
- Core account meetings expose booking, schedule, conference, recording, and transcript-presence fields. `meeting_call_recordings` is separate and currently has no Core-account or Phoebe-organization foreign key, so the admin-agent per-org Granola query needs a follow-up matching contract.
- The onboarding slice is intentionally separate from the admin-agent meeting-context slice.
- PHO-12880 implementation adds Core organization-scoped kickoff read/create routes, a member-authenticated Phoebe proxy, and a polling onboarding card that embeds the returned Calendly URL and renders scheduled/held state.
- The isolated worktree stack verified the card and retry state in a real browser, but Core was not configured in that stack (`CORE_API_BASE_URL`/token absent), so successful Calendly booking requires a Core-enabled CI or preview environment.
