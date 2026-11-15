"""Unit tests for image selection quality (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.image_relevance import (
    ImageCandidate,
    assess_image_attachment,
    normalize_image_url,
    rank_image_candidates,
    select_document_image,
)

SOURCE = "https://www.thehindu.com/news/national/story.ece"


def doc(url: str, image_url: str | None, **metadata: object) -> SimpleNamespace:
    meta = {"image_url": image_url, **metadata} if image_url else dict(metadata)
    return SimpleNamespace(url=url, title="Story", metadata_=meta)


# --- provenance ---------------------------------------------------------
def test_same_source_image_is_verified() -> None:
    decision = assess_image_attachment(
        image_url="https://www.thehindu.com/images/lead-photo.jpg", source_url=SOURCE
    )
    assert decision.attach is True
    assert decision.verified is True
    assert decision.reason == "source_article_image"


def test_known_publisher_cdn_counts_as_same_source() -> None:
    decision = assess_image_attachment(
        image_url="https://thgim.com/images/lead.jpg", source_url=SOURCE
    )
    assert decision.attach is True
    assert decision.verified is True


def test_cross_domain_photo_is_rejected() -> None:
    decision = assess_image_attachment(
        image_url="https://random-blog.example/photo.jpg", source_url=SOURCE
    )
    assert decision.attach is False
    assert decision.reason == "unrelated_or_unprovenanced_image"


def test_logo_and_avatar_are_rejected() -> None:
    for url in (
        "https://www.thehindu.com/assets/site-logo.png",
        "https://www.thehindu.com/img/author-avatar.jpg",
        "https://www.thehindu.com/img/sprite-icons.png",
    ):
        assert assess_image_attachment(image_url=url, source_url=SOURCE).attach is False


def test_tiny_image_dimensions_are_rejected() -> None:
    decision = assess_image_attachment(
        image_url="https://www.thehindu.com/images/lead.jpg",
        source_url=SOURCE,
        width=120,
        height=90,
    )
    assert decision.attach is False
    assert decision.reason == "image_too_small"


# --- normalisation / dedupe --------------------------------------------
def test_cdn_resize_parameters_collapse_to_one_image() -> None:
    assert normalize_image_url(
        "https://www.thehindu.com/a.jpg?w=1200&q=80"
    ) == normalize_image_url("https://www.thehindu.com/a.jpg?w=640")


def test_duplicate_images_collapse_and_keep_the_best_score() -> None:
    candidates = [
        ImageCandidate(
            image_url="https://www.thehindu.com/a.jpg?w=400",
            source_url=SOURCE,
            width=400,
            height=300,
            document_index=0,
        ),
        ImageCandidate(
            image_url="https://www.thehindu.com/a.jpg?w=1200",
            source_url=SOURCE,
            width=1200,
            height=675,
            document_index=1,
        ),
    ]
    ranked = rank_image_candidates(candidates)
    assert len(ranked) == 1
    assert ranked[0][1].image_url.endswith("w=1200")


# --- ranking ------------------------------------------------------------
def test_landscape_beats_portrait_thumbnail() -> None:
    candidates = [
        ImageCandidate(
            image_url="https://www.thehindu.com/portrait.jpg",
            source_url=SOURCE,
            width=400,
            height=700,
            document_index=0,
        ),
        ImageCandidate(
            image_url="https://www.thehindu.com/wide.jpg",
            source_url=SOURCE,
            width=1200,
            height=675,
            document_index=1,
        ),
    ]
    ranked = rank_image_candidates(candidates)
    assert ranked[0][1].image_url.endswith("wide.jpg")


def test_verified_beats_unprovenanced_same_shape() -> None:
    candidates = [
        # No source URL means provenance cannot be established at all.
        ImageCandidate(
            image_url="https://cdn.example/photo.jpg",
            source_url="",
            width=1200,
            height=675,
            document_index=0,
        ),
        ImageCandidate(
            image_url="https://www.thehindu.com/photo.jpg",
            source_url=SOURCE,
            width=1200,
            height=675,
            document_index=1,
        ),
    ]
    ranked = rank_image_candidates(candidates)
    assert ranked[0][2].verified is True
    assert ranked[0][1].image_url.startswith("https://www.thehindu.com")
    assert ranked[1][2].verified is False
    assert ranked[1][2].reason == "unverified_but_plausible"


def test_recently_used_photo_is_deprioritised() -> None:
    candidates = [
        ImageCandidate(
            image_url="https://www.thehindu.com/used.jpg",
            source_url=SOURCE,
            width=1200,
            height=675,
            document_index=0,
        ),
        ImageCandidate(
            image_url="https://www.thehindu.com/fresh.jpg",
            source_url=SOURCE,
            width=1200,
            height=675,
            document_index=1,
        ),
    ]
    ranked = rank_image_candidates(
        candidates, recent_image_urls=["https://www.thehindu.com/used.jpg?w=1200"]
    )
    assert ranked[0][1].image_url.endswith("fresh.jpg")


# --- document selection -------------------------------------------------
def test_best_image_is_chosen_across_documents_not_the_first() -> None:
    documents = [
        doc(SOURCE, "https://www.thehindu.com/images/thumb.jpg", image_width=300, image_height=300),
        doc("https://www.ndtv.com/story", "https://ndtvimg.com/lead.jpg", image_width=1280, image_height=720),
    ]
    decision = select_document_image(documents, "tanker attack")
    assert decision.attach is True
    assert decision.image_url == "https://ndtvimg.com/lead.jpg"
    assert decision.verified is True


def test_no_attachable_image_returns_no_image() -> None:
    documents = [
        doc(SOURCE, None),
        doc("https://www.thehindu.com/other", "https://elsewhere.example/x.jpg"),
    ]
    decision = select_document_image(documents, "topic")
    assert decision.attach is False
    assert decision.image_url is None


def test_dimensions_are_read_from_metadata() -> None:
    documents = [
        doc(
            SOURCE,
            "https://www.thehindu.com/images/lead.jpg",
            image_width=1200,
            image_height=675,
        )
    ]
    decision = select_document_image(documents, "topic")
    assert (decision.width, decision.height) == (1200, 675)
    assert decision.aspect_ratio and decision.aspect_ratio > 1.7
