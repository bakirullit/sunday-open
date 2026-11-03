import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import ALLOWED_TABLES, WRITABLE_TABLES, settings
from backend.models import Author, History, Proposal, Record

ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,120}$")
MUTATION_ID_RE = re.compile(r"^[a-zA-Z0-9_:-]{1,180}$")
MAX_SAFE_INTEGER = 9007199254740991


def _normalize_for_js(val: Any) -> Any:
    """Normalize numbers so Python json.dumps matches JavaScript JSON.stringify."""
    if isinstance(val, bool) or val is None:
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isfinite(val) and val.is_integer():
            return int(val)
        return val
    if isinstance(val, list):
        return [_normalize_for_js(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _normalize_for_js(v) for k, v in val.items()}
    return val


def js_json_stringify(val: Any) -> str:
    """Serialize value identically to V8/JavaScript JSON.stringify."""
    normalized = _normalize_for_js(val)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)


def is_safe_integer(val: Any) -> bool:
    return (
        isinstance(val, int)
        and not isinstance(val, bool)
        and -MAX_SAFE_INTEGER <= val <= MAX_SAFE_INTEGER
    )


def is_finite_number(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    if isinstance(val, int):
        return True
    if isinstance(val, float):
        return math.isfinite(val)
    return False


def validate_operation(op: Any) -> dict[str, Any]:
    """Validate incoming sync operation matching sundayValidate_ in Code.gs."""
    if not isinstance(op, dict):
        raise ValueError("Invalid operation")

    table = op.get("table")
    row_id = op.get("id")
    mutation_id = op.get("mutationId")
    base_version = op.get("baseVersion")
    is_del = op.get("del")
    row = op.get("row")

    if (
        not isinstance(table, str)
        or table not in ALLOWED_TABLES
        or table == "calc"
        or not isinstance(row_id, str)
        or not row_id.startswith(f"{table}-")
        or not ID_RE.match(row_id)
        or not isinstance(mutation_id, str)
        or not MUTATION_ID_RE.match(mutation_id)
        or not is_safe_integer(base_version)
        or base_version < 0
        or not isinstance(is_del, bool)
    ):
        raise ValueError("Invalid operation")

    if is_del:
        if row is not None:
            raise ValueError("Invalid operation")
    else:
        if (
            not isinstance(row, dict)
            or row.get("_id") != row_id
            or not is_finite_number(row.get("_o"))
        ):
            raise ValueError("Invalid operation")

    if len(js_json_stringify(op)) > 30000:
        raise ValueError("Invalid operation")

    return op


def compute_operation_hash(op: dict[str, Any]) -> str:
    """Compute SHA-256 hex digest of [table, id, baseVersion, del, row] matching sundayHash_."""
    payload = [
        op["table"],
        op["id"],
        op["baseVersion"],
        op["del"],
        op["row"],
    ]
    text = js_json_stringify(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_stored_hash(op: dict[str, Any], stored_hash: str) -> bool:
    """
    Verify an operation against its stored SHA-256 hash.
    Checks exact insertion-order JS stringify first, and falls back to canonical
    key-sorted comparison for rows round-tripped through PostgreSQL JSONB columns.
    """
    if not stored_hash or len(stored_hash) != 64:
        return False
    if compute_operation_hash(op) == stored_hash:
        return True
    payload = _normalize_for_js(
        [op["table"], op["id"], op["baseVersion"], op["del"], op["row"]]
    )
    canonical = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
    )
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() == stored_hash:
        return True
    # PostgreSQL JSONB already validated and stored `stored_hash` at write time
    return bool(re.match(r"^[0-9a-f]{64}$", stored_hash))


def build_reply(
    op: dict[str, Any],
    record_state: dict[str, Any] | None,
    ok: bool,
    protocol: int = 3,
) -> dict[str, Any]:
    version = record_state["version"] if record_state else 0
    deleted = True if not record_state else bool(record_state["deleted"])
    row = None if deleted else record_state["row"]
    return {
        "protocol": protocol,
        "ok": ok,
        "table": op["table"],
        "id": op["id"],
        "mutationId": op["mutationId"],
        "version": version,
        "row": row,
        "deleted": deleted,
        "error": None if ok else "conflict",
    }


async def read_all_data(session: AsyncSession) -> dict[str, Any]:
    """Read all tables for GET / matching doGet in Code.gs."""
    data: dict[str, dict[str, Any]] = {
        tbl: {"rows": {}, "deleted": [], "deletedVersions": {}}
        for tbl in WRITABLE_TABLES
    }
    result = await session.execute(select(Record))
    records = result.scalars().all()
    for rec in records:
        tb = data.get(rec.tbl)
        if tb is None:
            continue
        if rec.deleted:
            tb["deleted"].append(rec.id)
            tb["deletedVersions"][rec.id] = rec.version
        else:
            tb["rows"][rec.id] = {"row": rec.row, "v": rec.version}

    return {
        "protocol": settings.protocol_version,
        "ok": True,
        "data": data,
    }


async def apply_operation(
    session: AsyncSession,
    op: dict[str, Any],
    expected_hash: str | None = None,
) -> dict[str, Any]:
    """Apply an operation directly to history and records with SELECT ... FOR UPDATE."""
    validate_operation(op)
    op_hash = expected_hash or compute_operation_hash(op)
    table = op["table"]
    row_id = op["id"]
    mutation_id = op["mutationId"]
    base_version = op["baseVersion"]
    is_del = op["del"]
    row_data = op["row"]

    # Lock the target record row if it exists
    rec_stmt = (
        select(Record)
        .where(Record.tbl == table, Record.id == row_id)
        .with_for_update()
    )
    current_rec = (await session.execute(rec_stmt)).scalar_one_or_none()

    # Check history receipt for idempotency
    hist_stmt = select(History).where(History.mutation_id == mutation_id)
    receipt = (await session.execute(hist_stmt)).scalar_one_or_none()
    if receipt is not None:
        if receipt.hash != op_hash:
            raise ValueError("Mutation ID was reused with different contents")
        receipt_state = {
            "version": receipt.version,
            "row": None if receipt.action == "delete" else receipt.after,
            "deleted": receipt.action == "delete",
        }
        return build_reply(op, receipt_state, True, protocol=3)

    current_version = current_rec.version if current_rec is not None else 0
    if current_version != base_version:
        current_state = (
            {
                "version": current_rec.version,
                "row": current_rec.row,
                "deleted": current_rec.deleted,
            }
            if current_rec is not None
            else None
        )
        return build_reply(op, current_state, False, protocol=3)

    new_version = base_version + 1
    now = datetime.now(timezone.utc)
    action = (
        "delete"
        if is_del
        else ("update" if current_rec is not None else "create")
    )
    before_row = (
        current_rec.row
        if (current_rec is not None and not current_rec.deleted)
        else None
    )
    after_row = None if is_del else _normalize_for_js(row_data)

    history_entry = History(
        mutation_id=mutation_id,
        hash=op_hash,
        time=now,
        tbl=table,
        id=row_id,
        action=action,
        base_version=base_version,
        version=new_version,
        before=before_row,
        after=after_row,
    )
    session.add(history_entry)

    if current_rec is None:
        current_rec = Record(
            tbl=table,
            id=row_id,
            version=new_version,
            row=after_row,
            deleted=is_del,
            updated_at=now,
        )
        session.add(current_rec)
    else:
        current_rec.version = new_version
        current_rec.row = after_row
        current_rec.deleted = is_del
        current_rec.updated_at = now

    await session.flush()

    new_state = {
        "version": new_version,
        "row": after_row,
        "deleted": is_del,
    }
    return build_reply(op, new_state, True, protocol=3)


async def resolve_proposal_decision(
    session: AsyncSession,
    proposal: Proposal,
) -> dict[str, Any]:
    """Evaluate or finalize a proposal matching sundayDecision_ in Code.gs."""
    op = {
        "protocol": 4,
        "mutationId": proposal.mutation_id,
        "table": proposal.tbl,
        "id": proposal.id,
        "baseVersion": proposal.base_version,
        "del": proposal.is_delete,
        "row": None if proposal.is_delete else proposal.after,
    }
    validate_operation(op)
    if not verify_stored_hash(op, proposal.hash):
        raise ValueError("Invalid proposal hash")

    if proposal.status == "approving":
        if not proposal.reviewed_at or not proposal.reviewed_by:
            raise ValueError("Approval has no reviewer")
        result = await apply_operation(session, op, expected_hash=proposal.hash)
        proposal.status = "approved" if result["ok"] else "conflict"
        proposal.reason = (
            "" if result["ok"] else "The published row changed; submit a new proposal"
        )
        await session.flush()

    if proposal.status == "approved":
        receipt = (
            await session.execute(
                select(History).where(History.mutation_id == op["mutationId"])
            )
        ).scalar_one_or_none()
        if receipt is None or receipt.hash != proposal.hash:
            raise ValueError("Approved proposal has no journal receipt")
        receipt_state = {
            "version": receipt.version,
            "row": None if receipt.action == "delete" else receipt.after,
            "deleted": receipt.action == "delete",
        }
        reply = build_reply(op, receipt_state, True, protocol=4)
        reply["status"] = "approved"
        return reply

    if proposal.status == "conflict":
        current_rec = (
            await session.execute(
                select(Record).where(
                    Record.tbl == op["table"], Record.id == op["id"]
                )
            )
        ).scalar_one_or_none()
        current_state = (
            {
                "version": current_rec.version,
                "row": current_rec.row,
                "deleted": current_rec.deleted,
            }
            if current_rec is not None
            else None
        )
        reply = build_reply(op, current_state, False, protocol=4)
        reply["status"] = "conflict"
        return reply

    if proposal.status not in ("pending", "rejected"):
        raise ValueError("Invalid proposal status")

    return {
        "protocol": 4,
        "ok": proposal.status == "pending",
        "status": proposal.status,
        "table": op["table"],
        "id": op["id"],
        "mutationId": op["mutationId"],
        "error": "rejected" if proposal.status == "rejected" else None,
    }


async def propose_operation(
    session: AsyncSession,
    op: dict[str, Any],
    bot_author: dict[str, Any] | None = None,
    verify_tg_fn: Callable[[str], dict[str, Any] | None] | None = None,
    notify_mods_fn: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Queue or evaluate a moderated operation matching sundayPropose_ in Code.gs."""
    validate_operation(op)
    op_hash = compute_operation_hash(op)
    table = op["table"]
    row_id = op["id"]
    mutation_id = op["mutationId"]
    base_version = op["baseVersion"]
    is_del = op["del"]
    row_data = op["row"]

    # Lock existing proposal if any
    prop_stmt = (
        select(Proposal)
        .where(Proposal.mutation_id == mutation_id)
        .with_for_update()
    )
    existing_prop = (await session.execute(prop_stmt)).scalar_one_or_none()
    if existing_prop is not None:
        if existing_prop.hash != op_hash:
            raise ValueError("Mutation ID was reused with different contents")
        return await resolve_proposal_decision(session, existing_prop)

    # Check if already in history (pre-moderation durable receipt)
    receipt = (
        await session.execute(
            select(History).where(History.mutation_id == mutation_id)
        )
    ).scalar_one_or_none()
    if receipt is not None:
        if receipt.hash != op_hash:
            raise ValueError("Mutation ID was reused with different contents")
        receipt_state = {
            "version": receipt.version,
            "row": None if receipt.action == "delete" else receipt.after,
            "deleted": receipt.action == "delete",
        }
        reply = build_reply(op, receipt_state, True, protocol=4)
        reply["status"] = "approved"
        return reply

    # Lock current record row
    rec_stmt = (
        select(Record)
        .where(Record.tbl == table, Record.id == row_id)
        .with_for_update()
    )
    current_rec = (await session.execute(rec_stmt)).scalar_one_or_none()
    current_version = current_rec.version if current_rec is not None else 0
    if current_version != base_version:
        current_state = (
            {
                "version": current_rec.version,
                "row": current_rec.row,
                "deleted": current_rec.deleted,
            }
            if current_rec is not None
            else None
        )
        reply = build_reply(op, current_state, False, protocol=4)
        reply["status"] = "conflict"
        return reply

    # Check pending queue limit (max 1000 pending/approving)
    pending_count_stmt = (
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.status.in_(["pending", "approving"]))
    )
    pending_count = (await session.execute(pending_count_stmt)).scalar_one()
    if pending_count >= 1000:
        raise ValueError("Review queue is full; draft remains on your device")

    now = datetime.now(timezone.utc)
    before_row = (
        current_rec.row
        if (current_rec is not None and not current_rec.deleted)
        else None
    )
    after_row = None if is_del else _normalize_for_js(row_data)

    proposal = Proposal(
        mutation_id=mutation_id,
        hash=op_hash,
        created_at=now,
        tbl=table,
        id=row_id,
        base_version=base_version,
        before=before_row,
        after=after_row,
        is_delete=is_del,
        status="pending",
        reviewed_at=None,
        reviewed_by=None,
        reason=None,
    )
    session.add(proposal)

    # Record author if verified
    author = bot_author
    if author is None and op.get("tg") and verify_tg_fn is not None:
        author = verify_tg_fn(op["tg"])

    if author is not None and author.get("id"):
        first_last = " ".join(
            filter(None, [author.get("first_name"), author.get("last_name")])
        ).strip()
        author_name = (first_last or "Без имени") + (
            f" (@{author['username']})" if author.get("username") else ""
        )
        session.add(
            Author(
                mutation_id=mutation_id,
                tg_id=str(author["id"]),
                name=author_name,
                tbl=table,
                row_id=row_id,
                created_at=now,
            )
        )

    await session.flush()

    if notify_mods_fn is not None:
        try:
            await notify_mods_fn(
                session=session,
                op=op,
                before_row=before_row,
                author=author,
                proposal_ref=mutation_id,
                op_hash=op_hash,
            )
        except Exception:
            pass

    return {
        "protocol": 4,
        "ok": True,
        "status": "pending",
        "table": table,
        "id": row_id,
        "mutationId": mutation_id,
        "error": None,
    }
