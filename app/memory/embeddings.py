"""Configurable local embedding providers.

Production default is a Sentence Transformers model (local inference, no paid
embedding API). A deterministic hashing embedder exists for offline tests; it is
a real vector embedding (hashed bag-of-words, L2-normalised), not string matching,
but it has no semantic generalisation and must not be used in production.

Embeddings from different models are never mixed: every stored vector is tagged
with its model name and the retrieval path filters on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingError(RuntimeError):
    pass


class EmbeddingConfigurationError(EmbeddingError):
    """The configured embedding backend is unavailable or inconsistent."""


class EmbeddingDimensionMismatch(EmbeddingError):
    pass


class EmbeddingProvider(Protocol):
    model_name: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class HashingEmbeddings:
    """Deterministic test/dev embedder. Not semantically meaningful."""

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions
        self.model_name = f"hashing-v1-{dimensions}"
        self._cache: dict[str, list[float]] = {}

    def _embed_one(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        vector = [0.0] * self.dimensions
        counts = Counter(tokens)
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        return _l2_normalize(vector)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            if not text or not text.strip():
                raise EmbeddingError("cannot embed empty text")
            cached = self._cache.get(text)
            if cached is None:
                cached = self._embed_one(text)
                if len(self._cache) < 512:
                    self._cache[text] = cached
            results.append(cached)
        return results


class SentenceTransformerEmbeddings:
    """Lazy-loading Sentence Transformers provider (local CPU inference)."""

    def __init__(
        self,
        model_name: str,
        *,
        dimensions: int | None = None,
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self._requested_dimensions = dimensions
        self._device = device
        self._batch_size = batch_size
        self._model = None
        self.dimensions = dimensions or 0
        self._lock = asyncio.Lock()

    def _load_model(self):  # type: ignore[no-untyped-def]
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EmbeddingConfigurationError(
                "sentence-transformers is not installed; install requirements-embeddings.txt "
                "or set EMBEDDING_PROVIDER=hashing for local development"
            ) from exc
        try:
            return SentenceTransformer(self.model_name, device=self._device)
        except Exception as exc:  # pragma: no cover - model download/IO
            raise EmbeddingConfigurationError(
                f"failed to load embedding model {self.model_name!r}: {exc}"
            ) from exc

    async def _ensure_model(self):  # type: ignore[no-untyped-def]
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                model = await asyncio.to_thread(self._load_model)
                # sentence-transformers renamed this accessor; keep both working.
                dimension_getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
                model_dimensions = int(dimension_getter())
                if self._requested_dimensions and model_dimensions != self._requested_dimensions:
                    raise EmbeddingDimensionMismatch(
                        f"model {self.model_name!r} produces {model_dimensions} dimensions, "
                        f"config expects {self._requested_dimensions}"
                    )
                self.dimensions = model_dimensions
                self._model = model
                logger.info(
                    "embedding model loaded",
                    extra={"extra_fields": {"model": self.model_name, "dimensions": model_dimensions}},
                )
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text or not text.strip() for text in texts):
            raise EmbeddingError("cannot embed empty text")
        model = await self._ensure_model()
        vectors = await asyncio.to_thread(
            model.encode,
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors]


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "hashing":
        logger.warning(
            "using deterministic hashing embeddings (development only); "
            "set EMBEDDING_PROVIDER=sentence_transformers for production"
        )
        return HashingEmbeddings(settings.embedding_dimensions)
    return SentenceTransformerEmbeddings(
        settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
