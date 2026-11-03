from __future__ import annotations

import json

import pytest

from scripts.run_newsroom_pilot import PilotStopError, _load_manifest


def test_partial_retry_requires_explicit_opt_in(tmp_path) -> None:  # type: ignore[no-untyped-def]
    manifest = tmp_path / "retry.json"
    manifest.write_text(
        json.dumps({"candidates": [{"category": "indian_economy", "source_document_id": 258}]}),
        encoding="utf-8",
    )

    with pytest.raises(PilotStopError, match="exactly one candidate"):
        _load_manifest(manifest)

    candidates = _load_manifest(manifest, allow_partial_retry=True)
    assert [(candidate.category, candidate.source_document_id) for candidate in candidates] == [
        ("indian_economy", 258)
    ]


def test_partial_retry_rejects_unknown_or_duplicate_categories(tmp_path) -> None:  # type: ignore[no-untyped-def]
    manifest = tmp_path / "invalid-retry.json"
    manifest.write_text(
        json.dumps(
            {
                "candidates": [
                    {"category": "unknown", "source_document_id": 1},
                    {"category": "unknown", "source_document_id": 2},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PilotStopError, match="distinct supported categories"):
        _load_manifest(manifest, allow_partial_retry=True)
