"""Deterministic word-based chunking with overlap."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    word_count: int


def chunk_text(
    text: str,
    *,
    max_words: int = 180,
    overlap_words: int = 40,
    min_words: int = 12,
) -> list[Chunk]:
    """Split text into overlapping windows of words.

    Overlap preserves context that straddles a boundary. Trailing windows shorter
    than ``min_words`` are merged into the previous chunk instead of being dropped,
    which keeps short release notes intact.
    """
    if max_words <= 0:
        raise ValueError("max_words must be positive")
    if not 0 <= overlap_words < max_words:
        raise ValueError("overlap_words must be >= 0 and < max_words")
    words = text.split()
    if not words:
        return []
    if len(words) <= max_words:
        return [Chunk(index=0, text=" ".join(words), word_count=len(words))]

    step = max_words - overlap_words
    chunks: list[Chunk] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + max_words)
        window = words[start:end]
        if chunks and len(window) < min_words:
            previous = chunks[-1]
            merged = f"{previous.text} {' '.join(window)}"
            chunks[-1] = Chunk(
                index=previous.index,
                text=merged,
                word_count=previous.word_count + len(window),
            )
            break
        chunks.append(Chunk(index=len(chunks), text=" ".join(window), word_count=len(window)))
        if end >= len(words):
            break
        start += step
    return chunks
