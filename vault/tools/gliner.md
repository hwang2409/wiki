---
type: reference
tags: [tools, ml, nlp]
created: 2026-09-17
updated: 2026-09-17
---

# GLiNER2.5

Fastino's `fastino/gliner2.5-multi-v1` — 287M-param multilingual information-extraction model (mDeBERTa-v3-base, 4096-token window, Apache 2.0, ~594 MB FP16). One model, zero-shot with user-supplied labels: NER, text classification, relation extraction, structured record parsing (`extract_json`). Paper: arXiv 2507.18546. Repo: github.com/fastino-ai/GLiNER2.

## Local setup

- Lives in `~/me/fun/misc/gliner/` (uv venv, Python 3.12). Demo script: `try_gliner.py`.
- Install: `uv venv --python 3.12 && uv pip install "gliner2[local]" protobuf sentencepiece`

## Gotchas

- `gliner2[local]` extras MISS `protobuf` + `sentencepiece`. Fast-tokenizer load fails (`AttributeError: 'list' object has no attribute 'keys'` in transformers 4.57 `_set_model_specific_special_tokens`), then the slow-tokenizer fallback dies with `ImportError: requires the protobuf library`. Installing both packages fixes it.
- Pin Python 3.12 — torch lags 3.14 (system default).
- Use `AutoExtractor.from_pretrained(...)`, not legacy `GLiNER2.from_pretrained()`.
- Char offsets are half-open: `text[start:end] == entity["text"]`.

## Observed quality (2026-09-17 smoke test)

English/German/Spanish NER, sentiment classification, and relation triples all correct on toy inputs. `extract_json` wobbles: split one person into two partial records (name/profession/employer vs age/email).
