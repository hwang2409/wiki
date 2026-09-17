---
type: reference
tags: [projects]
created: 2026-09-17
updated: 2026-09-17
---

# Splitty

Project to extract dropout-free instrumentals from ordinary lossy songs, without an official instrumental. Home: `~/me/fun/splitty/` (empty except HANDOFF.md). Session resume doc: `~/me/fun/splitty/HANDOFF.md` — read it first; it holds full state, decisions, and traps.

## State (2026-09-17)

- Test track: "Braces" by fakemink, in `~/me/fun/misc/demucs/` (.opus master, 129 kbps).
- Four separators run (htdemucs, htdemucs_ft, BS-RoFormer ep_317, MelBand Inst V2). All leave dropouts under vocals.
- Root cause locked: lossy encoding discards vocal-masked instrumental detail; discriminative separators cannot reconstruct it. Fix requires a generative component.
- **Tier 1 pipeline BUILT and RUN (09-17 ~14:21 EDT).** Apollo is NOT in audio-separator (v0.47.0) — cloned github.com/JusperLee/Apollo to `~/me/fun/splitty/Apollo`, venv via uv (py3.10, `requirements-macos-arm64.txt`, torch 2.11 MPS). Official HF checkpoint auto-downloaded. Full track restored in ~38 s on MPS: 6 s chunks, 1 s overlap, 1 s edge pad. Output: `~/me/fun/splitty/out/tier1/braces_inst_v2_apollo.wav` (float32 WAV, full length, finite; RMS diff 0.0136 vs input — real change, not pass-through).
- Spectrogram check: Apollo fills high-frequency content (>16 kHz now continuous) and softens the hard vertical dropout gaps; the 38-63 s striping remains visible but shallower. Listening verdict PENDING — Henry's ears decide Tier 1.
- A/B tool: `~/me/fun/splitty/ab.sh START DUR FILE_A FILE_B` (ffplay segment compare).

## Roadmap (decision, with rationale in HANDOFF.md)

1. Tier 1 (next): chain Apollo restoration (github.com/JusperLee/Apollo) after the Inst V2 instrumental.
2. Tier 2: self-train the repair model on destruction-generated data (clean -> mix -> encode -> separate -> learn damaged->clean).
3. Tier 3: end-to-end generative separation. Research territory; only if 1-2 disappoint.

## Gotchas

- demucs and audio-separator both need dep injection under uv (`--with "numpy<2"`, `--with audioread`).
- Stale yt-dlp (>90 days) gets YouTube 403s.

