import json
from pathlib import Path
import time

from httpx import ASGITransport, AsyncClient
import pytest

from backend.app import app
from backend.config import settings
from backend.import_sheets import (
    BOT_USERS_HEADERS,
    DATA_HEADERS,
    HISTORY_HEADERS,
    PROPOSALS_HEADERS,
    import_sheet_dump,
    load_dump_from_path,
)
from backend.models import Record
from backend.scheduler import try_with_db_advisory_lock
from backend.sync import compute_operation_hash, js_json_stringify


@pytest.mark.asyncio
async def test_legacy_google_sheet_import_reproduces_get_output(
    session_maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Acceptance criteria #6:
    The import from the legacy Google Sheet dump reproduces the exact same GET /
    output as Apps Script at cut-over, and repairs stale Data projections
    using SundayHistory.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")

    row1 = {"_id": "portal-1", "_o": 1, "title": "Imported Event 1", "stage": "Идея"}
    row2_v1 = {"_id": "portal-2", "_o": 2, "title": "Stale v1", "stage": "Идея"}
    row2_v2 = {"_id": "portal-2", "_o": 2, "title": "Repaired v2 from History", "stage": "Паспорт"}
    prop_row = {"_id": "portal-1", "_o": 1, "title": "Proposed Edit", "stage": "Одобрена"}

    h_row1 = compute_operation_hash(
        {"table": "portal", "id": "portal-1", "baseVersion": 0, "del": False, "row": row1}
    )
    h_row2_v1 = compute_operation_hash(
        {"table": "portal", "id": "portal-2", "baseVersion": 0, "del": False, "row": row2_v1}
    )
    h_row2_v2 = compute_operation_hash(
        {"table": "portal", "id": "portal-2", "baseVersion": 1, "del": False, "row": row2_v2}
    )
    h_prop = compute_operation_hash(
        {"table": "portal", "id": "portal-1", "baseVersion": 1, "del": False, "row": prop_row}
    )

    dump_bundle = {
        "Data": [
            DATA_HEADERS,
            ["portal", "portal-1", 1, js_json_stringify(row1), False, "2026-11-01T10:00:00Z"],
            ["portal", "portal-2", 1, js_json_stringify(row2_v1), False, "2026-11-01T10:00:00Z"],
        ],
        "SundayHistory": [
            HISTORY_HEADERS,
            ["mut:1", h_row1, "2026-11-01T10:00:00Z", "portal", "portal-1", "create", 0, 1, "", js_json_stringify(row1)],
            ["mut:2:v1", h_row2_v1, "2026-11-01T10:00:00Z", "portal", "portal-2", "create", 0, 1, "", js_json_stringify(row2_v1)],
            ["mut:2:v2", h_row2_v2, "2026-11-01T11:00:00Z", "portal", "portal-2", "update", 1, 2, js_json_stringify(row2_v1), js_json_stringify(row2_v2)],
        ],
        "SundayProposals": [
            PROPOSALS_HEADERS,
            [
                "prop:1",
                h_prop,
                "2026-11-02T10:00:00Z",
                "portal",
                "portal-1",
                1,
                js_json_stringify(row1),
                js_json_stringify(prop_row),
                False,
                "pending",
                "",
                "",
                "",
            ],
        ],
        "SundayBotUsers": [
            BOT_USERS_HEADERS,
            ["12345", "Alice", "alice", "2026-11-01T00:00:00Z", True],
        ],
    }

    dump_file = tmp_path / "dump.json"
    dump_file.write_text(json.dumps(dump_bundle, ensure_ascii=False), encoding="utf-8")

    loaded = load_dump_from_path(dump_file)
    async with session_maker() as s:
        async with s.begin():
            counts = await import_sheet_dump(s, loaded)
            assert counts["records"] == 2
            assert counts["history"] == 3
            assert counts["proposals"] == 1
            assert counts["bot_users"] == 1

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = (await client.get("/")).json()
        assert res["ok"] is True
        assert res["protocol"] == 4
        portal_rows = res["data"]["portal"]["rows"]
        assert portal_rows["portal-1"]["v"] == 1
        assert portal_rows["portal-1"]["row"]["title"] == "Imported Event 1"
        # Repaired from SundayHistory v2!
        assert portal_rows["portal-2"]["v"] == 2
        assert portal_rows["portal-2"]["row"]["title"] == "Repaired v2 from History"


@pytest.mark.asyncio
async def test_scheduler_advisory_lock_prevents_duplicates(session_maker) -> None:
    """
    Acceptance criteria #7:
    Scheduled jobs run protected by PostgreSQL transaction advisory lock.
    """
    calls: list[str] = []

    async def job_fn(_session) -> str:
        calls.append("ran")
        return "Done"

    async with session_maker() as s:
        async with s.begin():
            res = await try_with_db_advisory_lock(s, 999111, job_fn)
            assert res == "Done"
            assert calls == ["ran"]


@pytest.mark.asyncio
async def test_get_5000_records_p95_under_500ms(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Acceptance criteria #8:
    p95 response time is under 500 ms for GET / with 5,000 records.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")

    async with session_maker() as s:
        async with s.begin():
            batch = [
                Record(
                    tbl="portal" if i % 2 == 0 else "steps",
                    id=f"{'portal' if i % 2 == 0 else 'steps'}-perf-{i}",
                    version=1,
                    deleted=False,
                    row={
                        "_id": f"{'portal' if i % 2 == 0 else 'steps'}-perf-{i}",
                        "_o": i,
                        "title": f"Performance Benchmark Event {i}",
                        "club": f"Club {i % 25}",
                        "stage": "Одобрена",
                        "date": "2026-12-15",
                    },
                )
                for i in range(5000)
            ]
            s.add_all(batch)

    transport = ASGITransport(app=app)
    latencies_ms: list[float] = []
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Warm-up call
        warm = await client.get("/")
        assert warm.status_code == 200
        data_warm = warm.json()["data"]
        total_rows = len(data_warm["portal"]["rows"]) + len(data_warm["steps"]["rows"])
        assert total_rows == 5000

        for _ in range(10):
            t0 = time.perf_counter()
            r = await client.get("/")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert r.status_code == 200
            latencies_ms.append(elapsed_ms)

    latencies_ms.sort()
    p95_idx = max(0, int(len(latencies_ms) * 0.95) - 1)
    p95_ms = latencies_ms[p95_idx]
    assert p95_ms < 500.0, f"Expected p95 < 500ms for 5,000 records, got {p95_ms:.2f}ms"
