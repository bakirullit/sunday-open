import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import async_session_factory
from backend.models import (
    Author,
    BotUser,
    Contact,
    EmailNotified,
    History,
    KeyValue,
    Proposal,
    Record,
    Rsvp,
)
from backend.sync import (
    compute_operation_hash,
    validate_operation,
)

DATA_HEADERS = ["table", "id", "version", "json", "deleted", "updatedAt"]
HISTORY_HEADERS = [
    "mutationId",
    "hash",
    "time",
    "table",
    "id",
    "action",
    "baseVersion",
    "version",
    "before",
    "after",
]
PROPOSALS_HEADERS = [
    "mutationId",
    "hash",
    "createdAt",
    "table",
    "id",
    "baseVersion",
    "before",
    "after",
    "delete",
    "status",
    "reviewedAt",
    "reviewedBy",
    "reason",
]
NOTIFICATIONS_HEADERS = ["mutationId", "notifiedAt"]
BOT_USERS_HEADERS = ["tgId", "name", "username", "startedAt", "reminders"]
AUTHORS_HEADERS = ["mutationId", "tgId", "name", "table", "rowId", "createdAt"]
CONTACTS_HEADERS = [
    "tgId",
    "firstName",
    "lastName",
    "username",
    "phone",
    "email",
    "club",
    "role",
    "updatedAt",
]
RSVP_HEADERS = [
    "code",
    "eventId",
    "title",
    "date",
    "club",
    "tgId",
    "name",
    "status",
    "createdAt",
    "checkedInAt",
    "checkedBy",
]


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().upper() == "TRUE"
    return False


def _parse_dt(val: Any, allow_none: bool = False) -> datetime | None:
    if val is None or val == "":
        return None if allow_none else datetime.now(timezone.utc)
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
    s = str(val).strip()
    if not s:
        return None if allow_none else datetime.now(timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None if allow_none else datetime.now(timezone.utc)


def _parse_json_cell(val: Any) -> dict[str, Any] | None:
    if val is None or val == "":
        return None
    if isinstance(val, dict):
        return val
    s = str(val).strip()
    if not s:
        return None
    parsed = json.loads(s)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object in cell")
    return parsed


def _strip_sheet_rows(
    sheet_rows: list[list[Any]],
    expected_headers: list[str] | None = None,
) -> list[list[Any]]:
    if not sheet_rows:
        return []
    first = [str(c) for c in sheet_rows[0][: len(expected_headers)]] if expected_headers else []
    if expected_headers and first != expected_headers:
        raise ValueError(
            f"Unexpected headers: got {first}, expected {expected_headers}"
        )
    return sheet_rows[1:]


async def import_sheet_dump(
    session: AsyncSession,
    dump: dict[str, Any],
) -> dict[str, int]:
    """
    Import a Google Sheet data dump into PostgreSQL.
    Preserves versions, history, proposals, notifications, bot_users, authors, contacts, rsvp, and kv.
    Matches sundayRead_ invariants: validates rows and repairs Data projection from SundayHistory if needed.
    """
    counts = {
        "records": 0,
        "history": 0,
        "proposals": 0,
        "email_notified": 0,
        "bot_users": 0,
        "authors": 0,
        "contacts": 0,
        "rsvp": 0,
        "kv": 0,
    }

    records_state: dict[str, dict[str, Any]] = {}
    receipts_state: dict[str, dict[str, Any]] = {}

    # 1. Parse Data sheet
    if "Data" in dump and dump["Data"]:
        rows = _strip_sheet_rows(dump["Data"], DATA_HEADERS)
        for line in rows:
            if not line or (not line[0] and (len(line) < 2 or not line[1])):
                continue
            padded = list(line) + [""] * (6 - len(line))
            tbl = str(padded[0])
            row_id = str(padded[1])
            version = int(padded[2])
            deleted = _parse_bool(padded[4])
            row_obj = None if deleted else _parse_json_cell(padded[3])
            updated_at = _parse_dt(padded[5]) or datetime.now(timezone.utc)

            validate_operation(
                {
                    "table": tbl,
                    "id": row_id,
                    "baseVersion": version - 1,
                    "del": deleted,
                    "row": row_obj,
                    "mutationId": "validate",
                }
            )
            key = f"{tbl}|{row_id}"
            if key in records_state:
                raise ValueError(f"Duplicate Data row: {key}")
            records_state[key] = {
                "tbl": tbl,
                "id": row_id,
                "version": version,
                "row": row_obj,
                "deleted": deleted,
                "updated_at": updated_at,
            }

    # 2. Parse SundayHistory sheet and repair Data projection if needed
    if "SundayHistory" in dump and dump["SundayHistory"]:
        rows = _strip_sheet_rows(dump["SundayHistory"], HISTORY_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (10 - len(line))
            mutation_id = str(padded[0])
            op_hash = str(padded[1])
            time_dt = _parse_dt(padded[2]) or datetime.now(timezone.utc)
            tbl = str(padded[3])
            row_id = str(padded[4])
            action = str(padded[5])
            base_version = int(padded[6])
            version = int(padded[7])
            before_obj = _parse_json_cell(padded[8])
            after_obj = None if action == "delete" else _parse_json_cell(padded[9])
            deleted = action == "delete"

            op = {
                "table": tbl,
                "id": row_id,
                "baseVersion": base_version,
                "del": deleted,
                "row": after_obj,
                "mutationId": mutation_id,
            }
            validate_operation(op)
            if (
                version != base_version + 1
                or compute_operation_hash(op) != op_hash
                or mutation_id in receipts_state
            ):
                raise ValueError("Invalid SundayHistory entry")

            rec_snapshot = {
                "tbl": tbl,
                "id": row_id,
                "version": version,
                "row": after_obj,
                "deleted": deleted,
                "updated_at": time_dt,
            }
            receipts_state[mutation_id] = {
                "hash": op_hash,
                "record": rec_snapshot,
                "time": time_dt,
                "action": action,
                "base_version": base_version,
                "before": before_obj,
                "after": after_obj,
            }

            key = f"{tbl}|{row_id}"
            current = records_state.get(key)
            if current and current["version"] >= version:
                continue
            if (current["version"] if current else 0) != base_version:
                raise ValueError(f"Gap in journal: {key}")
            records_state[key] = rec_snapshot

    # Persist History entries
    for mutation_id, h in receipts_state.items():
        existing_h = (
            await session.execute(
                select(History).where(History.mutation_id == mutation_id)
            )
        ).scalar_one_or_none()
        rec = h["record"]
        if existing_h is None:
            session.add(
                History(
                    mutation_id=mutation_id,
                    hash=h["hash"],
                    time=h["time"],
                    tbl=rec["tbl"],
                    id=rec["id"],
                    action=h["action"],
                    base_version=h["base_version"],
                    version=rec["version"],
                    before=h["before"],
                    after=h["after"],
                )
            )
            counts["history"] += 1

    # Persist Record entries
    for rec in records_state.values():
        existing_r = (
            await session.execute(
                select(Record).where(
                    Record.tbl == rec["tbl"], Record.id == rec["id"]
                )
            )
        ).scalar_one_or_none()
        if existing_r is None:
            session.add(
                Record(
                    tbl=rec["tbl"],
                    id=rec["id"],
                    version=rec["version"],
                    row=rec["row"],
                    deleted=rec["deleted"],
                    updated_at=rec["updated_at"],
                )
            )
        else:
            existing_r.version = rec["version"]
            existing_r.row = rec["row"]
            existing_r.deleted = rec["deleted"]
            existing_r.updated_at = rec["updated_at"]
        counts["records"] += 1

    # 3. Parse SundayProposals sheet
    if "SundayProposals" in dump and dump["SundayProposals"]:
        rows = _strip_sheet_rows(dump["SundayProposals"], PROPOSALS_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (13 - len(line))
            mutation_id = str(padded[0])
            op_hash = str(padded[1])
            created_at = _parse_dt(padded[2]) or datetime.now(timezone.utc)
            tbl = str(padded[3])
            row_id = str(padded[4])
            base_version = int(padded[5])
            before_obj = _parse_json_cell(padded[6])
            is_delete = _parse_bool(padded[8])
            after_obj = None if is_delete else _parse_json_cell(padded[7])
            status = str(padded[9])
            reviewed_at = _parse_dt(padded[10], allow_none=True)
            reviewed_by = str(padded[11]) if padded[11] != "" else None
            reason = str(padded[12]) if padded[12] != "" else None

            op = {
                "protocol": 4,
                "mutationId": mutation_id,
                "table": tbl,
                "id": row_id,
                "baseVersion": base_version,
                "del": is_delete,
                "row": after_obj,
            }
            validate_operation(op)
            if compute_operation_hash(op) != op_hash:
                raise ValueError("Invalid proposal hash")

            existing_p = (
                await session.execute(
                    select(Proposal).where(Proposal.mutation_id == mutation_id)
                )
            ).scalar_one_or_none()
            if existing_p is None:
                session.add(
                    Proposal(
                        mutation_id=mutation_id,
                        hash=op_hash,
                        created_at=created_at,
                        tbl=tbl,
                        id=row_id,
                        base_version=base_version,
                        before=before_obj,
                        after=after_obj,
                        is_delete=is_delete,
                        status=status,
                        reviewed_at=reviewed_at,
                        reviewed_by=reviewed_by,
                        reason=reason,
                    )
                )
            else:
                existing_p.status = status
                existing_p.reviewed_at = reviewed_at
                existing_p.reviewed_by = reviewed_by
                existing_p.reason = reason
            counts["proposals"] += 1

    # 4. Parse SundayNotifications sheet
    if "SundayNotifications" in dump and dump["SundayNotifications"]:
        rows = _strip_sheet_rows(dump["SundayNotifications"], NOTIFICATIONS_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (2 - len(line))
            mutation_id = str(padded[0])
            notified_at = _parse_dt(padded[1]) or datetime.now(timezone.utc)
            existing_n = (
                await session.execute(
                    select(EmailNotified).where(
                        EmailNotified.mutation_id == mutation_id
                    )
                )
            ).scalar_one_or_none()
            if existing_n is None:
                session.add(
                    EmailNotified(
                        mutation_id=mutation_id,
                        notified_at=notified_at,
                    )
                )
                counts["email_notified"] += 1

    # 5. Parse SundayBotUsers sheet
    if "SundayBotUsers" in dump and dump["SundayBotUsers"]:
        rows = _strip_sheet_rows(dump["SundayBotUsers"], BOT_USERS_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (5 - len(line))
            tg_id = str(padded[0])
            name = str(padded[1])
            username = str(padded[2] or "")
            started_at = _parse_dt(padded[3]) or datetime.now(timezone.utc)
            reminders = _parse_bool(padded[4]) if padded[4] != "" else True
            existing_u = (
                await session.execute(
                    select(BotUser).where(BotUser.tg_id == tg_id)
                )
            ).scalar_one_or_none()
            if existing_u is None:
                session.add(
                    BotUser(
                        tg_id=tg_id,
                        name=name,
                        username=username,
                        started_at=started_at,
                        reminders=reminders,
                    )
                )
            else:
                existing_u.name = name
                existing_u.username = username
                existing_u.reminders = reminders
            counts["bot_users"] += 1

    # 6. Parse SundayAuthors sheet
    if "SundayAuthors" in dump and dump["SundayAuthors"]:
        rows = _strip_sheet_rows(dump["SundayAuthors"], AUTHORS_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (6 - len(line))
            mutation_id = str(padded[0])
            tg_id = str(padded[1])
            name = str(padded[2])
            tbl = str(padded[3])
            row_id = str(padded[4])
            created_at = _parse_dt(padded[5]) or datetime.now(timezone.utc)
            existing_a = (
                await session.execute(
                    select(Author).where(Author.mutation_id == mutation_id)
                )
            ).scalar_one_or_none()
            if existing_a is None:
                session.add(
                    Author(
                        mutation_id=mutation_id,
                        tg_id=tg_id,
                        name=name,
                        tbl=tbl,
                        row_id=row_id,
                        created_at=created_at,
                    )
                )
                counts["authors"] += 1

    # 7. Parse SundayContacts sheet
    if "SundayContacts" in dump and dump["SundayContacts"]:
        rows = _strip_sheet_rows(dump["SundayContacts"], CONTACTS_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (9 - len(line))
            tg_id = str(padded[0])
            first_name = str(padded[1] or "")
            last_name = str(padded[2] or "")
            username = str(padded[3] or "")
            phone = str(padded[4] or "").lstrip("'")
            email = str(padded[5] or "")
            club = str(padded[6] or "")
            role = str(padded[7] or "")
            updated_at = _parse_dt(padded[8]) or datetime.now(timezone.utc)

            existing_c = (
                await session.execute(
                    select(Contact).where(Contact.tg_id == tg_id)
                )
            ).scalar_one_or_none()
            if existing_c is None:
                session.add(
                    Contact(
                        tg_id=tg_id,
                        first_name=first_name,
                        last_name=last_name,
                        username=username,
                        phone=phone,
                        email=email,
                        club=club,
                        role=role,
                        updated_at=updated_at,
                    )
                )
            else:
                existing_c.first_name = first_name
                existing_c.last_name = last_name
                existing_c.username = username
                existing_c.phone = phone
                existing_c.email = email
                existing_c.club = club
                existing_c.role = role
                existing_c.updated_at = updated_at
            counts["contacts"] += 1

    # 8. Parse SundayRSVP sheet
    if "SundayRSVP" in dump and dump["SundayRSVP"]:
        rows = _strip_sheet_rows(dump["SundayRSVP"], RSVP_HEADERS)
        for line in rows:
            if not line or not line[0]:
                continue
            padded = list(line) + [""] * (11 - len(line))
            code = str(padded[0])
            event_id = str(padded[1])
            title = str(padded[2])
            date_str = str(padded[3] or "").lstrip("'")
            if "T" in date_str:
                date_str = date_str.split("T")[0]
            club = str(padded[4] or "")
            tg_id = str(padded[5])
            name = str(padded[6])
            status = str(padded[7])
            created_at = _parse_dt(padded[8]) or datetime.now(timezone.utc)
            checked_in_at = _parse_dt(padded[9], allow_none=True)
            checked_by = str(padded[10]) if padded[10] != "" else None

            existing_rsvp = (
                await session.execute(
                    select(Rsvp).where(Rsvp.code == code)
                )
            ).scalar_one_or_none()
            if existing_rsvp is None:
                session.add(
                    Rsvp(
                        code=code,
                        event_id=event_id,
                        title=title,
                        date=date_str,
                        club=club,
                        tg_id=tg_id,
                        name=name,
                        status=status,
                        created_at=created_at,
                        checked_in_at=checked_in_at,
                        checked_by=checked_by,
                    )
                )
            else:
                existing_rsvp.status = status
                existing_rsvp.checked_in_at = checked_in_at
                existing_rsvp.checked_by = checked_by
            counts["rsvp"] += 1

    # 9. Parse kv / properties
    props = dump.get("properties") or dump.get("kv") or {}
    if isinstance(props, dict):
        for k, v in props.items():
            existing_kv = (
                await session.execute(
                    select(KeyValue).where(KeyValue.key == str(k))
                )
            ).scalar_one_or_none()
            val_str = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            if existing_kv is None:
                session.add(KeyValue(key=str(k), value=val_str))
            else:
                existing_kv.value = val_str
            counts["kv"] += 1

    await session.flush()
    return counts


def load_dump_from_path(input_path: Path) -> dict[str, Any]:
    """Load a Google Sheet dump from a JSON file or a directory of CSV files."""
    if input_path.is_file():
        return json.loads(input_path.read_text(encoding="utf-8"))
    if input_path.is_dir():
        dump: dict[str, Any] = {}
        for csv_file in sorted(input_path.glob("*.csv")):
            sheet_name = csv_file.stem
            with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                dump[sheet_name] = [list(row) for row in reader]
        props_file = input_path / "properties.json"
        if props_file.exists():
            dump["properties"] = json.loads(props_file.read_text(encoding="utf-8"))
        return dump
    raise FileNotFoundError(f"Input path not found: {input_path}")


async def _run_cli(input_path: Path) -> None:
    dump = load_dump_from_path(input_path)
    async with async_session_factory() as session:
        async with session.begin():
            counts = await import_sheet_dump(session, dump)
    print(f"Import completed successfully: {json.dumps(counts)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Sunday Open Google Sheet dump into PostgreSQL"
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        type=Path,
        help="Path to JSON dump file or directory containing sheet CSV files",
    )
    args = parser.parse_args()
    asyncio.run(_run_cli(args.input))


if __name__ == "__main__":
    main()

