from __future__ import annotations

import pytest

from app.ingestion.extract import clean_excerpt, extract_text
from app.memory.chunking import chunk_text


def test_chunking_short_text_is_single_chunk() -> None:
    chunks = chunk_text("short technical note", max_words=100, overlap_words=10)
    assert len(chunks) == 1
    assert chunks[0].word_count == 3


def test_chunking_overlaps_and_covers_all_words() -> None:
    text = " ".join(f"w{i}" for i in range(100))
    chunks = chunk_text(text, max_words=30, overlap_words=10, min_words=5)
    assert len(chunks) == 5
    assert chunks[0].text.split()[-10:] == chunks[1].text.split()[:10]
    combined = set(" ".join(c.text for c in chunks).split())
    assert combined == set(text.split())
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunking_rejects_invalid_overlap() -> None:
    with pytest.raises(ValueError):
        chunk_text("a b c", max_words=10, overlap_words=10)


def test_chunking_merges_short_tail() -> None:
    text = " ".join(f"w{i}" for i in range(26))
    chunks = chunk_text(text, max_words=20, overlap_words=5, min_words=10)
    assert chunks[-1].word_count >= 10


def test_extract_text_drops_scripts_and_keeps_title() -> None:
    html = """
    <html><head><title>Release notes</title><style>.x{color:red}</style></head>
    <body><script>alert('x')</script><h1>v2.0</h1><p>Faster &amp; smaller.</p>
    <noscript>ignore</noscript></body></html>
    """
    text, title = extract_text(html)
    assert title == "Release notes"
    assert "alert" not in text
    assert "color:red" not in text
    assert "Faster & smaller." in text
    assert "ignore" not in text


def test_clean_excerpt_truncates_on_word_boundary() -> None:
    excerpt = clean_excerpt("word " * 200, limit=50)
    assert len(excerpt) <= 51
    assert excerpt.endswith("…")
