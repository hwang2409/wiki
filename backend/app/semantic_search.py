"""Embedding providers and compact vector helpers for vault semantic search."""

from __future__ import annotations

import json
import math
import os
import struct
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingUnavailable(RuntimeError):
    """Raised when semantic search cannot obtain a valid embedding."""


class EmbeddingProvider(Protocol):
    """Small provider contract used by production and deterministic tests."""

    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def normalise_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise EmbeddingUnavailable("embedding provider returned an invalid vector")
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude <= 0:
        raise EmbeddingUnavailable("embedding provider returned a zero vector")
    return [value / magnitude for value in values]


def pack_vector(vector: Sequence[float]) -> bytes:
    """Store normalized vectors as bounded, portable float32 blobs."""

    normalized = normalise_vector(vector)
    return struct.pack(f"<{len(normalized)}f", *normalized)


def unpack_vector(blob: bytes, dimension: int) -> list[float]:
    if dimension <= 0 or len(blob) != dimension * 4:
        raise EmbeddingUnavailable("stored embedding has an invalid shape")
    return list(struct.unpack(f"<{dimension}f", blob))


def cosine_normalized(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingUnavailable("embedding dimensions do not match")
    return sum(float(a) * float(b) for a, b in zip(left, right, strict=True))


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embeddings provider with a short bounded timeout."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.openai.com/v1/embeddings",
        model: str = "text-embedding-3-small",
        timeout_seconds: float = 8.0,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.timeout_seconds = max(1.0, min(timeout_seconds, 30.0))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps(
            {"model": self.model, "input": list(texts)},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.load(response)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise EmbeddingUnavailable(f"embedding provider request failed: {exc}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingUnavailable("embedding provider returned an invalid response")
        rows = sorted(
            (row for row in body["data"] if isinstance(row, dict)),
            key=lambda row: int(row.get("index", 0)),
        )
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
            raise EmbeddingUnavailable("embedding provider returned the wrong number of vectors")
        return [normalise_vector(vector) for vector in vectors]


def configured_provider(env: dict[str, str] | None = None) -> EmbeddingProvider | None:
    """Return the configured provider, or ``None`` for lexical-only mode."""

    values = os.environ if env is None else env
    # A dedicated key is required. Never infer consent from a generic provider key.
    api_key = (values.get("WIKI_EMBEDDINGS_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        timeout = float(values.get("WIKI_EMBEDDINGS_TIMEOUT", "8"))
    except ValueError:
        timeout = 8.0
    return OpenAIEmbeddingProvider(
        api_key,
        endpoint=values.get(
            "WIKI_EMBEDDINGS_BASE_URL",
            "https://api.openai.com/v1/embeddings",
        ),
        model=values.get("WIKI_EMBEDDINGS_MODEL", "text-embedding-3-small"),
        timeout_seconds=timeout,
    )


def provider_model(provider: Any) -> str:
    return str(getattr(provider, "model", provider.__class__.__name__))
