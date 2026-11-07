"""Embedders (ADR-010). Both produce L2-normalised 384-dim vectors.

- FastEmbedder: fastembed BAAI/bge-small-en-v1.5 (local ONNX). The model is read from
  FASTEMBED_CACHE_PATH; the worker image bakes it in at build time, so runtime is offline.
- HashEmbedder: deterministic bag-of-words (each word hashed to a signed dimension). Not
  semantic: similarity is word overlap. For tests and offline dev, where predictable scores
  matter more than meaning.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from typing import Any, Protocol

from agentops.settings import EMBEDDING_DIM, Settings

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashEmbedder:
    name = "hash-bow-v1"

    def _one(self, text: str) -> list[float]:
        v = [0.0] * EMBEDDING_DIM
        for w in _WORD.findall(text.lower()):
            h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "big")
            v[h % EMBEDDING_DIM] += 1.0 if (h >> 32) & 1 else -1.0
        return _normalise(v)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class FastEmbedder:
    def __init__(self, settings: Settings) -> None:
        self.name = settings.embedding_model
        self._settings = settings
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding  # noqa: PLC0415  (heavy import, only if used)

            self._model = TextEmbedding(
                model_name=self._settings.embedding_model,
                cache_dir=str(self._settings.fastembed_cache_path),
            )
        return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [_normalise([float(x) for x in v]) for v in model.embed(texts)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)


def make_embedder(settings: Settings) -> Embedder:
    return HashEmbedder() if settings.embedding_mode == "hash" else FastEmbedder(settings)
