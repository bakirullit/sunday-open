import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import History, Record
from backend.sync import compute_operation_hash

logger = logging.getLogger("sunday_open.seed")

SEED_FILE_PATH = Path(__file__).resolve().parent / "seed_data.json"


async def ensure_seed_data(session: AsyncSession) -> int:
    """
    Ensure initial Workspace (DEF: 15 tables) and Portal (SEED: portal table)
    records exist in PostgreSQL so the backend is the single source of truth
    for both the web frontends and the Telegram bot.
    Only inserts rows whose (tbl, id) are not yet present in `records`.
    """
    if not SEED_FILE_PATH.is_file():
        logger.warning("Seed data file not found at %s", SEED_FILE_PATH)
        return 0

    try:
        seed_payload: dict[str, list[dict[str, Any]]] = json.loads(
            SEED_FILE_PATH.read_text(encoding="utf-8")
        )
    except Exception as exc:
        logger.error("Failed to parse seed_data.json: %s", exc)
        return 0

    existing_res = await session.execute(select(Record.tbl, Record.id))
    existing_keys = {(r[0], r[1]) for r in existing_res.all()}

    existing_hist_res = await session.execute(select(History.mutation_id))
    existing_mutations = {r[0] for r in existing_hist_res.all()}

    now = datetime.now(timezone.utc)
    inserted = 0

    for tbl, rows in seed_payload.items():
        if not isinstance(rows, list):
            continue
        for idx, row_obj in enumerate(rows):
            if not isinstance(row_obj, dict):
                continue
            row_id = str(row_obj.get("_id") or f"{tbl}-{idx}")
            if (tbl, row_id) in existing_keys:
                continue

            clean_row = {**row_obj, "_id": row_id}
            if "_o" not in clean_row:
                clean_row["_o"] = idx

            mutation_id = f"seed:{tbl}:{row_id}"
            op_hash = compute_operation_hash(
                {
                    "table": tbl,
                    "id": row_id,
                    "baseVersion": 0,
                    "del": False,
                    "row": clean_row,
                }
            )

            session.add(
                Record(
                    tbl=tbl,
                    id=row_id,
                    version=1,
                    row=clean_row,
                    deleted=False,
                    updated_at=now,
                )
            )
            existing_keys.add((tbl, row_id))

            if mutation_id not in existing_mutations:
                session.add(
                    History(
                        mutation_id=mutation_id,
                        hash=op_hash,
                        time=now,
                        tbl=tbl,
                        id=row_id,
                        action="upsert",
                        base_version=0,
                        version=1,
                        before=None,
                        after=clean_row,
                    )
                )
                existing_mutations.add(mutation_id)

            inserted += 1

    if inserted > 0:
        await session.flush()
        logger.info("Seeded %d initial records into PostgreSQL.", inserted)

    return inserted
