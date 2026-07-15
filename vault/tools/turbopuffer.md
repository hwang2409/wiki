---
type: reference
tags: [tools, search]
created: 2026-07-15
updated: 2026-07-15
---

# Turbopuffer

Serverless vector + full-text search database built on object storage (S3/GCS): cold data durable and cheap in object storage, hot data cached on NVMe/RAM. ~10-100x cheaper than RAM-resident vector DBs (Pinecone-class) for large or mostly-cold datasets.

## Core model

- Storage-compute separation: writes land in object storage; queries served from cache layer.
- Namespaces = many small isolated indexes, cheap at millions of tenants — per-user / per-doc / per-repo search is the native shape.
- Search: ANN vector + BM25 full-text + attribute filters; hybrid queries.
- Plain HTTP API (upsert/query), fully managed — no infra to run.

## When to use / avoid

| Fit | Anti-fit |
|---|---|
| Many tenants, spiky access, cost-sensitive | Single always-hot index |
| RAG / embeddings per customer | Ultra-low latency (<10ms p99) constant workloads |
| Big corpus, mostly cold | — |

- Tradeoff: cold-namespace first query pays object-storage fetch latency.
- Marquee users: Cursor (codebase indexing), Notion, Linear.

Source: general knowledge as of 2026-07; verify pricing/limits at turbopuffer.com before building on it.
