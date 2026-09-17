---
type: til
tags: [wiki-app/til]
created: 2026-08-25
updated: 2026-08-25
---

# render_artifact mp4 ctts cap

`render_artifact` video payloads reject mp4s whose `ctts` (composition time-to-sample) table exceeds 4096 entries: `video payload rejected: mp4 ctts entry count exceeds 4096` (media_scrub rebuild bound). B-frame-heavy encodes — including chimy2's newt demo renders — hit this.

Fix: transcode a copy without B-frames before rendering:

```bash
ffmpeg -i in.mp4 -c:v libx264 -profile:v baseline -bf 0 -pix_fmt yuv420p -movflags +faststart -an out.mp4
```

Baseline profile forbids B-frames, so the ctts table disappears. Hit 2026-08-25 rendering `newt/demos/reel.mp4` (NEWT-35 reel).

Also: `path` payloads only accept the vault/runtime/archive roots — stage outside files under `~/.wiki/agent-runtime/tmp/` (and delete the scratch copy after; the artifact store keeps its own rebuilt bytes).

Related: [[newt-demo-convention]].

