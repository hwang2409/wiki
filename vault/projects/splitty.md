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
- Spectrogram check: Apollo fills high-frequency content (>16 kHz now continuous) and softens the hard vertical dropout gaps; the 38-63 s striping remains visible but shallower.
- **Henry's verdict on stem-restore chain: NOT good enough (09-17).**
- **Tier 1.5 experiments run (09-17 ~14:27 EDT):**
  - Restore-first chain (in-domain for Apollo — it trained on compressed MIXTURES, not stems): opus mix -> 44.1k WAV (`out/braces_mix_44k.wav`) -> Apollo 12 s chunks (~30 s) -> inst_v2 separation (~1 min). Output: `out/tier1/restore_first/braces_mix_apollo_(Instrumental)_melband_roformer_inst_v2.flac`. Spectrogram: densest mid/high band of all candidates; 38-63 s stripes narrower but still present.
  - Stem-restore with 12 s chunks (more context): `out/tier1/braces_inst_v2_apollo_12s.wav`. Barely different from the 6 s version.
- **Henry's verdict on BOTH Tier 1 orders (official checkpoint): NOT good enough (09-17).** Community fine-tunes approved (trust call made).
- **Lew Universal Lossy Enhancer run (09-17 ~14:37 EDT).** Checkpoint + config from deton24's GitHub release (uni tag) at `~/me/fun/splitty/models/apollo_model_uni.ckpt`. Surprise: the ckpt is ALREADY in Apollo serialized format (`model_name`/`state_dict`, no `model.` prefixes) — no base_model.py patch needed. Its embedded `model_args` is bogus (`n_sample_rate: 2`); constructor args come from the CLI. `inference.py` gained a `--feature-dim` flag (this model needs 384; official stays 256 default). Both orders run with 19 s chunks / 2 s overlap / 1 s pad on MPS (~80 s each):
  - (a) stem-restore: `out/lew_uni/braces_inst_v2_lewuni.wav` — densest, most uniform candidate yet; band filled to 22 kHz; 38-63 s dropout columns much shallower than baseline.
  - (b) restore-first: `out/lew_uni/restore_first/braces_mix_lewuni_(Instrumental)_melband_roformer_inst_v2.flac` — dense mids, striping still faintly visible above ~6 kHz; similar to official restore-first, slightly fuller top end.
  - All outputs verified: 96.49 s, finite, 44.1 kHz. Spectrograms (full + 38-63 s focus) in `out/spectro/lew_uni/`.
- **Henry's verdict on Lew Universal (09-17): BETTER than official, but dampening remains where vocals overlap densely.**
- **Anti-dampening round (09-17 ~14:50 EDT), two new candidates:**
  - (c) full chain: mix -> Lew -> inst_v2 -> Lew again. `out/lew_uni/braces_full_chain_lewuni.wav`.
  - (d) max-spec ensemble of (a)+(c): per STFT bin (n_fft 4096, hop 1024), keep the louder candidate's complex value. `out/lew_uni/braces_maxspec_a_c.wav` — fullest spectrogram of all candidates; inter-transient dropout columns mostly filled. Classic UVR anti-dropout trick; risk is extra vocal bleed where one source leaks.
  - Both verified 96.49 s, finite, 44.1 kHz. Spectrograms in `out/spectro/lew_uni/` (c_/d_ prefixes).
- **Henry's verdict on (c)/(d) (09-17): (d) still dampens on overlaps.** Diagnosis: both ensemble sources share the inst_v2 separator, so overlap bins fail identically — ensemble diversity needs a different separator family or a generative model.
- **Repo created (09-17 ~15:00 EDT): github.com/hwang2409/splitty (private).** `~/me/fun/splitty` is now a git repo: README (manifest schema), seeded `manifest.json` (11 entries: references/intermediates/candidates with pipeline+params+spectrogram paths), `scripts/maxspec.py` (generalized N-way max-spec ensemble), `patches/apollo-local.patch` (the --feature-dim inference patch). `out/`, `models/`, `Apollo/` gitignored — audio stays local.
- **Two worker lanes spawned (09-17 ~15:01 EDT, orch splitty):**
  - SPLIT-1 (cdx luna high): remaining Tier 1 lever — up to 3 more community Apollo fine-tunes (deton24 releases, jarredou, Baicai1145, HF), both orders each, PLUS the separator-diversity ensemble: Lew-restore the BS-RoFormer stem, max-spec it against the inst_v2 chains (different separator = different failure bins; the direct anti-overlap lever). Appends manifest entries + results/SPLIT-1.md via PR.
  - SPLIT-2 (cc opus-4.7): "Splitty Lab" web app — FastAPI+uvicorn on 127.0.0.1:8321, vanilla JS dark UI, manifest-driven catalog (pipeline chips, params, spectrogram lightbox), inline playback with Range support, A/B deck (shared transport, instant X-switch preserving playhead, 38/25 s loop). PR lane.
- After the lanes: Henry listens; if overlap dampening still stands, Tier 2 (destruction-generated training pairs target exactly this vocal-masked loss).
- A/B tool: `~/me/fun/splitty/ab.sh START DUR FILE_A FILE_B` (ffplay segment compare).

## Roadmap (decision, with rationale in HANDOFF.md)

1. Tier 1 (next): chain Apollo restoration (github.com/JusperLee/Apollo) after the Inst V2 instrumental.
2. Tier 2: self-train the repair model on destruction-generated data (clean -> mix -> encode -> separate -> learn damaged->clean).
3. Tier 3: end-to-end generative separation. Research territory; only if 1-2 disappoint.

## Gotchas

- demucs and audio-separator both need dep injection under uv (`--with "numpy<2"`, `--with audioread`).
- Stale yt-dlp (>90 days) gets YouTube 403s.

