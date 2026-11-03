from __future__ import annotations

from sqlalchemy import select

from app.memory.database import session_scope
from app.memory.models import Post
from app.memory.repositories import DraftRepository


async def create_reply_draft(
    account_id: int, key: str, *, status: str = "needs_review"
) -> int:
    async with session_scope() as session:
        draft = await DraftRepository.create(
            session,
            account_id=account_id,
            kind="reply",
            status=status,
            idempotency_key=key,
            topic="reply to @devperson",
            body="The scheduler change is the interesting part.",
            thread_parts=[],
            in_reply_to_post_id="555",
            target_author_handle="devperson",
            verification_report={"passed": True, "unverified_facts": 0},
            policy_report={"duplicate": {"duplicate": False}},
        )
        await session.commit()
        return draft.id


async def test_drafts_api_requires_auth(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get("/drafts")).status_code == 401
    assert (
        await client.get("/drafts", headers={"Authorization": "Bearer wrong"})
    ).status_code == 403


async def test_list_and_get_reply_drafts(client, admin_headers, account) -> None:  # type: ignore[no-untyped-def]
    draft_id = await create_reply_draft(account.id, "api-1")
    response = await client.get("/drafts", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == draft_id
    assert body["account"] == account.handle

    detail = await client.get(f"/drafts/{draft_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["verification"]["passed"] is True
    assert (await client.get("/drafts/99999", headers=admin_headers)).status_code == 404


async def test_approve_then_reject_flow(client, admin_headers, account) -> None:  # type: ignore[no-untyped-def]
    draft_id = await create_reply_draft(account.id, "api-2")
    approved = await client.post(
        f"/drafts/{draft_id}/approve",
        headers=admin_headers,
        json={"reviewer": "alice", "notes": "good point"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewed_by"] == "alice"

    duplicate_approve = await client.post(
        f"/drafts/{draft_id}/approve", headers=admin_headers, json={"reviewer": "bob"}
    )
    assert duplicate_approve.status_code == 409

    rejected = await client.post(
        f"/drafts/{draft_id}/reject",
        headers=admin_headers,
        json={"reviewer": "bob", "notes": "tone is off"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["review_notes"] == "tone is off"

    async with session_scope() as session:
        posts = (await session.scalars(select(Post))).all()
    assert posts == []


async def test_reject_requires_reason(client, admin_headers, account) -> None:  # type: ignore[no-untyped-def]
    draft_id = await create_reply_draft(account.id, "api-3")
    response = await client.post(
        f"/drafts/{draft_id}/reject", headers=admin_headers, json={"reviewer": "x", "notes": ""}
    )
    assert response.status_code == 422


async def test_drafts_are_account_scoped(client, admin_headers, account_factory, account) -> None:
    other = await account_factory("otherperson", "9999")
    foreign_draft = await create_reply_draft(other.id, "foreign-1")
    response = await client.get("/drafts", headers=admin_headers)
    ids = [item["id"] for item in response.json()["items"]]
    assert foreign_draft not in ids
    assert (
        await client.get(f"/drafts/{foreign_draft}", headers=admin_headers)
    ).status_code == 404


async def test_no_publish_endpoint_exists(client, admin_headers, account) -> None:  # type: ignore[no-untyped-def]
    draft_id = await create_reply_draft(account.id, "api-4")
    response = await client.post(f"/drafts/{draft_id}/publish", headers=admin_headers)
    assert response.status_code in {404, 405}
