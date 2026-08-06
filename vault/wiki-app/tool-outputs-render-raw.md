---
type: reference
tags: [wiki-app, doctrine, transcript]
created: 2026-08-06
updated: 2026-08-06
---

# Tool outputs render raw; rich rendering only via explicit render_artifact

**Rule.** Tool output blocks in the transcript render their content raw — text stays text, JSON stays JSON, URLs are links. NO implicit rich rendering: no unfurl cards, no auto-mermaid, no auto-image, no table promotion, no fence-tag → artifact swap. If the agent wants a rich render, it emits `render_artifact` explicitly as its own turn.

**Why (Henry 2026-08-06).**
- Tool output is signal from the tool. Adding a presentation layer on top obscures the signal — you have to read past the chrome to see what happened.
- Consistency: WIKI-252 stripped `GhPreviewCard` from tool output on the same principle ("i like the idea, but sometimes when a github URL is in a tool response, it gets kind of hard to read"). Applying the rule everywhere means one predictable behavior across tool outputs.
- Agents already have the escape hatch. `render_artifact` is first-class, chosen by the agent, not implicit. When rich rendering is intended, use it. When it isn't, don't.
- Prose and message bodies are exempt — that's presentation surface, not signal. Preview cards, autolinks, and markdown rendering all stay in prose.

**How to apply.**
- Reject any proposal to auto-promote content in tool output: fence-tag → mermaid, image-path → `<img>`, N-row table → sortable artifact, github URL → unfurl card. All variants of the same anti-pattern.
- Keep the render_artifact path first-class and easy to invoke — the more painless the explicit path, the less pressure there is to add implicit auto-promotion.
- The rule scopes to TOOL OUTPUT rendering surfaces. Chat message bodies, notes, wiki pages, and vault docs render prose normally (markdown, previews, everything).
- Wrap and readability rules (WIKI-251) still apply — raw output should be readable inline, not scroll-only.

**Related.**
- [[opencode-design-direction]], [[opencode-transcript-doctrine]] — the "solid, unambiguous hierarchy" direction this rule supports.
- WIKI-252 PR #188 — killed `GhPreviewCard` in tool output (the original application of this rule).
- [[skip-review-ui-wiki]] — pure UI/polish tickets skip deep-review; this note is about content policy, not process.
