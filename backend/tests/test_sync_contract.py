from datetime import datetime, timezone
import json
from typing import Any

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from backend.app import app
from backend.config import settings
from backend.models import History, Proposal, Record
from backend.moderation import review_proposal
from backend.sync import (
    compute_operation_hash,
    js_json_stringify,
    validate_operation,
)


@pytest.mark.asyncio
async def test_js_stringify_and_hash_parity() -> None:
    """Verify JS JSON.stringify and SHA-256 hex digest parity with Apps Script."""
    row = {"_id": "portal-test-1", "_o": 1700000000000.0, "title": "Хакатон", "stage": "Идея"}
    s = js_json_stringify(row)
    assert s == '{"_id":"portal-test-1","_o":1700000000000,"title":"Хакатон","stage":"Идея"}'

    op1 = {
        "table": "portal",
        "id": "portal-test-1",
        "baseVersion": 0,
        "del": False,
        "row": row,
    }
    h1 = compute_operation_hash(op1)
    assert len(h1) == 64


@pytest.mark.asyncio
async def test_operation_validation_invariants() -> None:
    """Test all §7.1 validation rules (calc rejection, regexes, row size, _id, _o)."""
    # Valid operation passes
    valid_op = {
        "table": "portal",
        "id": "portal-1",
        "mutationId": "mut:1",
        "baseVersion": 0,
        "del": False,
        "row": {"_id": "portal-1", "_o": 100},
    }
    assert validate_operation(valid_op) == valid_op

    # Disallowed table (calc)
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {"table": "calc", "id": "calc-1", "mutationId": "m1", "baseVersion": 0, "del": False, "row": {"_id": "calc-1", "_o": 1}}
        )

    # Row id does not start with table-
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {"table": "portal", "id": "other-1", "mutationId": "m1", "baseVersion": 0, "del": False, "row": {"_id": "other-1", "_o": 1}}
        )

    # Bad mutationId characters
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {"table": "portal", "id": "portal-1", "mutationId": "bad mut!", "baseVersion": 0, "del": False, "row": {"_id": "portal-1", "_o": 1}}
        )

    # Negative baseVersion
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {"table": "portal", "id": "portal-1", "mutationId": "m1", "baseVersion": -1, "del": False, "row": {"_id": "portal-1", "_o": 1}}
        )

    # Row mismatch _id
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {
                "table": "portal",
                "id": "portal-1",
                "mutationId": "m1",
                "baseVersion": 0,
                "del": False,
                "row": {"_id": "portal-other", "_o": 1},
            }
        )

    # Row missing finite _o
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {
                "table": "portal",
                "id": "portal-1",
                "mutationId": "m1",
                "baseVersion": 0,
                "del": False,
                "row": {"_id": "portal-1", "_o": "not-a-number"},
            }
        )

    # Row > 30,000 chars
    with pytest.raises(ValueError, match="Invalid operation"):
        validate_operation(
            {
                "table": "portal",
                "id": "portal-1",
                "mutationId": "m1",
                "baseVersion": 0,
                "del": False,
                "row": {"_id": "portal-1", "_o": 1, "big": "x" * 30500},
            }
        )


@pytest.mark.asyncio
async def test_protocol_3_direct_sync_contract_and_idempotency(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Acceptance criteria #1, #2, #3:
    Verify protocol 3 (direct mode) GET / and POST / over text/plain,
    CORS headers, version progression, conflict handling, and mutationId idempotency.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "3")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Initial GET /
        r_get = await client.get("/")
        assert r_get.status_code == 200
        assert r_get.headers["access-control-allow-origin"] == "*"
        assert "application/json" in r_get.headers["content-type"]
        body = r_get.json()
        assert body["ok"] is True
        assert body["protocol"] == 3
        assert "portal" in body["data"]
        assert "calc" not in body["data"]
        assert body["data"]["portal"] == {"rows": {}, "deleted": [], "deletedVersions": {}}

        # 2. Wrong protocol -> upgrade_required
        r_wrong_proto = await client.post(
            "/",
            content=json.dumps({"protocol": 4, "table": "portal", "id": "portal-101"}),
            headers={"Content-Type": "text/plain;charset=utf-8"},
        )
        assert r_wrong_proto.json() == {"protocol": 3, "ok": False, "error": "upgrade_required"}

        # 3. Direct POST / via Content-Type: text/plain
        op1 = {
            "protocol": 3,
            "table": "portal",
            "id": "portal-101",
            "mutationId": "mut:101:v1",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-101", "_o": 1000, "title": "First Idea", "stage": "Идея"},
        }
        r_post1 = await client.post(
            "/",
            content=json.dumps(op1),
            headers={"Content-Type": "text/plain;charset=utf-8"},
        )
        assert r_post1.status_code == 200
        res1 = r_post1.json()
        assert res1 == {
            "protocol": 3,
            "ok": True,
            "table": "portal",
            "id": "portal-101",
            "mutationId": "mut:101:v1",
            "version": 1,
            "row": op1["row"],
            "deleted": False,
            "error": None,
        }

        # 4. Replay identical mutationId -> idempotent return without duplicate history row
        r_post1_dup = await client.post(
            "/",
            content=json.dumps(op1),
            headers={"Content-Type": "text/plain"},
        )
        assert r_post1_dup.json() == res1

        async with session_maker() as s:
            hist_rows = (
                await s.execute(select(History).where(History.mutation_id == "mut:101:v1"))
            ).scalars().all()
            assert len(hist_rows) == 1

        # 5. Reused mutationId with different content -> error
        op1_tampered = {
            **op1,
            "row": {"_id": "portal-101", "_o": 1000, "title": "Tampered", "stage": "Идея"},
        }
        r_tampered = await client.post("/", content=json.dumps(op1_tampered))
        assert r_tampered.json()["ok"] is False
        assert "Mutation ID was reused with different contents" in r_tampered.json()["error"]

        # 6. Stale baseVersion -> conflict with current server state
        op_conflict = {
            "protocol": 3,
            "table": "portal",
            "id": "portal-101",
            "mutationId": "mut:101:stale",
            "baseVersion": 0,  # server is at version 1
            "del": False,
            "row": {"_id": "portal-101", "_o": 1000, "title": "Stale Edit", "stage": "Идея"},
        }
        r_conflict = await client.post("/", content=json.dumps(op_conflict))
        assert r_conflict.json() == {
            "protocol": 3,
            "ok": False,
            "table": "portal",
            "id": "portal-101",
            "mutationId": "mut:101:stale",
            "version": 1,
            "row": op1["row"],
            "deleted": False,
            "error": "conflict",
        }


@pytest.mark.asyncio
async def test_protocol_4_moderation_concurrent_proposals_and_approving_recovery(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Acceptance criteria #1 & #3:
    - Protocol 4 queues proposals as pending.
    - Two concurrent proposals on the same row: one is approved (version -> 1),
      the second automatically becomes 'conflict' when reviewed or polled.
    - Interrupted 'approving' proposal where history already exists is finalized cleanly.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        prop_a = {
            "protocol": 4,
            "table": "portal",
            "id": "portal-race-1",
            "mutationId": "prop:race:a",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-race-1", "_o": 10, "title": "Variant A", "stage": "Идея"},
        }
        prop_b = {
            "protocol": 4,
            "table": "portal",
            "id": "portal-race-1",
            "mutationId": "prop:race:b",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-race-1", "_o": 10, "title": "Variant B", "stage": "Идея"},
        }

        res_a = (await client.post("/", content=json.dumps(prop_a))).json()
        res_b = (await client.post("/", content=json.dumps(prop_b))).json()
        assert res_a["status"] == "pending"
        assert res_b["status"] == "pending"

        async def _noop_send(*_a: Any, **_kw: Any) -> None:
            return None

        # Approve Proposal A
        async with session_maker() as s:
            async with s.begin():
                rev_a = await review_proposal(
                    s,
                    proposal_ref="prop:race:a",
                    approve=True,
                    reviewer="moderator@sunday",
                    send_author_fn=_noop_send,
                )
                assert rev_a["status"] == "approved"

        # Attempt to approve Proposal B -> must result in conflict
        async with session_maker() as s:
            async with s.begin():
                rev_b = await review_proposal(
                    s,
                    proposal_ref="prop:race:b",
                    approve=True,
                    reviewer="moderator@sunday",
                    send_author_fn=_noop_send,
                )
                assert rev_b["status"] == "conflict"

        # Re-posting prop_a returns 'approved', re-posting prop_b returns 'conflict'
        poll_a = (await client.post("/", content=json.dumps(prop_a))).json()
        poll_b = (await client.post("/", content=json.dumps(prop_b))).json()
        assert poll_a["status"] == "approved"
        assert poll_a["version"] == 1
        assert poll_b["ok"] is False
        assert poll_b["error"] == "conflict"
        assert poll_b["status"] == "conflict"
        assert poll_b["version"] == 1

        # Simulate interrupted 'approving' proposal whose history receipt was already written
        async with session_maker() as s:
            async with s.begin():
                row_rec = (
                    await s.execute(
                        select(Record).where(
                            Record.tbl == "portal", Record.id == "portal-race-1"
                        )
                    )
                ).scalar_one()
                op_c = {
                    "protocol": 4,
                    "table": "portal",
                    "id": "portal-race-1",
                    "mutationId": "prop:interrupted:c",
                    "baseVersion": 1,
                    "del": False,
                    "row": {"_id": "portal-race-1", "_o": 10, "title": "Variant C", "stage": "Паспорт"},
                }
                h_c = compute_operation_hash(op_c)
                now = datetime.now(timezone.utc)
                row_rec.version = 2
                row_rec.row = op_c["row"]
                s.add(
                    History(
                        mutation_id="prop:interrupted:c",
                        hash=h_c,
                        time=now,
                        tbl="portal",
                        id="portal-race-1",
                        action="update",
                        base_version=1,
                        version=2,
                        before=prop_a["row"],
                        after=op_c["row"],
                    )
                )
                s.add(
                    Proposal(
                        mutation_id="prop:interrupted:c",
                        hash=h_c,
                        created_at=now,
                        tbl="portal",
                        id="portal-race-1",
                        base_version=1,
                        before=prop_a["row"],
                        after=op_c["row"],
                        is_delete=False,
                        status="approving",
                        reviewed_at=now,
                        reviewed_by="moderator@sunday",
                        reason="",
                    )
                )

            # Polling the interrupted 'approving' proposal recovers and finalizes it to 'approved'
            poll_c = (await client.post("/", content=json.dumps(op_c))).json()
            assert poll_c["status"] == "approved"
            assert poll_c["version"] == 2
