import base64
import hashlib
import hmac
import io
import json
import random
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import httpx
import segno
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models import (
    Author,
    BotUser,
    Contact,
    Proposal,
    Record,
    Rsvp,
)
from backend.moderation import (
    TG_LABELS,
    get_kv,
    get_tg_moderators,
    is_tg_moderator,
    notify_moderators_on_propose,
    review_proposal,
    send_tg_api,
    send_tg_message,
    set_kv,
    tg_cut,
    tg_date_fmt,
    tg_esc,
    tg_name,
    tg_webapp_kb,
)
from backend.sync import apply_operation, propose_operation

TG_STEPS: list[tuple[str, str]] = [
    ("title", "Как называется мероприятие?"),
    ("club", "Какой клуб или команда подаёт идею?"),
    ("date", "Когда? Дата в формате ДД.ММ. Если пока неизвестно, отправьте «-»."),
    ("what", "What — что проводите? Формат и суть в 2–3 предложениях."),
    ("why", "Why — зачем? Что получат участники?"),
    ("who", "Who — для кого и кто организует? Сколько людей ждёте?"),
    ("where", "Where — где? Зал или площадка, вместимость."),
    ("how", "How — как проводите? Программа, этапы, тайминг."),
    (
        "need",
        "Что потребуется от штаба? Помещение, техника, деньги, продвижение, партнёры.",
    ),
]
TG_LIVE: tuple[str, ...] = ("Одобрена", "Паспорт", "Утверждён", "Подготовка")
TG_EARLY: tuple[str, ...] = ("Идея",)
TG_OLD_STAGE: dict[str, str] = {
    "На доработке": "Идея",
    "Паспорт на проверке": "Паспорт",
    "Go": "Утверждён",
}
TG_ROLES: tuple[str, ...] = ("Участник", "Организатор", "Волонтёр")
IDEA_STAGE_ORDER: tuple[str, ...] = (
    "Идея",
    "Одобрена",
    "Паспорт",
    "Утверждён",
    "Подготовка",
    "Отложен",
    "Проведён",
)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EVENT_ID_RE = re.compile(r"^portal-[a-zA-Z0-9_-]{1,100}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHORT_DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")
CMD_RE = re.compile(r"^/(\w+)(?:@\w+)?\s*([\s\S]*)$")
CHECKIN_ARG_RE = re.compile(r"^ci_[a-z0-9]{6,20}$")
MOD_CB_RE = re.compile(r"^([ar]):(.+):([0-9a-f]{10})$")


class StateStore:
    """Dialog & deduplication state store backed by Redis with in-memory TTL fallback."""

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client
        self._mem: dict[str, tuple[str, float]] = {}

    async def get(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                val = await self._redis.get(key)
                if val is not None:
                    return val.decode("utf-8") if isinstance(val, bytes) else str(val)
            except Exception:
                pass
        entry = self._mem.get(key)
        if entry is None:
            return None
        val, exp = entry
        if time.time() > exp:
            self._mem.pop(key, None)
            return None
        return val

    async def put(self, key: str, value: str, ttl: int = 21600) -> None:
        self._mem[key] = (value, time.time() + ttl)
        if self._redis is not None:
            try:
                await self._redis.set(key, value, ex=ttl)
            except Exception:
                pass

    async def remove(self, key: str) -> None:
        self._mem.pop(key, None)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception:
                pass


default_state_store = StateStore()


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.TZ)
    except Exception:
        return ZoneInfo("Asia/Almaty")


def tg_today() -> str:
    return datetime.now(get_tz()).strftime("%Y-%m-%d")


def tg_days_from_today(iso_date: str, today_iso: str | None = None) -> int:
    base_today = today_iso or tg_today()
    d1 = datetime.strptime(iso_date, "%Y-%m-%d").date()
    d0 = datetime.strptime(base_today, "%Y-%m-%d").date()
    return (d1 - d0).days


def tg_parse_date(s: Any, today_iso: str | None = None) -> str | None:
    text = str(s or "").strip()
    if ISO_DATE_RE.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            return None
    m = SHORT_DATE_RE.match(text)
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y_raw = m.group(3)
    y = (2000 + int(y_raw) if int(y_raw) < 100 else int(y_raw)) if y_raw else 0
    if mo < 1 or mo > 12 or d < 1 or d > 31:
        return None

    base_today = today_iso or tg_today()
    if not y:
        y = int(base_today[:4])
        candidate = f"{y:04d}-{mo:02d}-{d:02d}"
        if candidate < base_today:
            y += 1
    iso = f"{y:04d}-{mo:02d}-{d:02d}"
    try:
        datetime.strptime(iso, "%Y-%m-%d")
        return iso
    except ValueError:
        return None


# ===================== §8.1 initData Verification =====================

def verify_init_data(
    init_data: Any,
    bot_token: str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """
    Verify Telegram WebApp initData signature matching §8.1 and tgVerify_ in Code.gs.
    """
    token = bot_token if bot_token is not None else settings.TG_BOT_TOKEN
    if (
        not token
        or not isinstance(init_data, str)
        or not init_data
        or len(init_data) > 4096
    ):
        return None

    hash_val = ""
    data_pairs: list[str] = []
    params: dict[str, str] = {}

    for part in init_data.split("&"):
        idx = part.find("=")
        if idx < 0:
            continue
        k = unquote(part[:idx].replace("+", " "))
        v = unquote(part[idx + 1 :].replace("+", " "))
        if k == "hash":
            hash_val = v
        else:
            data_pairs.append(f"{k}={v}")
            params[k] = v

    if not hash_val:
        return None

    data_pairs.sort()
    data_check_string = "\n".join(data_pairs)

    secret = hmac.new(
        b"WebAppData", token.encode("utf-8"), hashlib.sha256
    ).digest()
    check = hmac.new(
        secret, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(check, hash_val):
        return None

    current_time = now_ts if now_ts is not None else time.time()
    try:
        auth_date = float(params.get("auth_date") or 0)
    except (ValueError, TypeError):
        return None

    if current_time - auth_date > 7 * 86400:
        return None

    try:
        user = json.loads(params.get("user") or "null")
        return user if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None


# ===================== QR Code Generation (segno) =====================

def generate_qr_png_bytes(code: str, bot_username: str) -> bytes:
    deep_link = f"https://t.me/{bot_username}?start=ci_{code}"
    qr = segno.make(deep_link, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=10, border=3)
    return buf.getvalue()


def generate_qr_data_uri(code: str, bot_username: str) -> str:
    png_bytes = generate_qr_png_bytes(code, bot_username)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def send_tg_qr_photo(
    chat_id: int | str,
    code: str,
    caption: str,
    bot_username: str,
    bot_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    token = bot_token if bot_token is not None else settings.TG_BOT_TOKEN
    if not token:
        return None
    png_bytes = generate_qr_png_bytes(code, bot_username)
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
    }
    files = {"photo": (f"qr_{code}.png", png_bytes, "image/png")}
    try:
        if http_client is not None:
            resp = await http_client.post(url, data=data, files=files, timeout=15.0)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, data=data, files=files)
        return resp.json()
    except Exception:
        return None


async def send_tg_csv_document(
    chat_id: int | str,
    filename: str,
    csv_text: str,
    caption: str,
    bot_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    token = bot_token if bot_token is not None else settings.TG_BOT_TOKEN
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
    }
    files = {"document": (filename, csv_text.encode("utf-8"), "text/csv")}
    try:
        if http_client is not None:
            resp = await http_client.post(url, data=data, files=files, timeout=15.0)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, data=data, files=files)
        return resp.json()
    except Exception:
        return None


# ===================== Contacts, Profile & RSVP (§9) =====================

def contact_to_profile(contact: Contact | None, user: dict[str, Any] | None = None) -> dict[str, str]:
    if contact is not None:
        return {
            "firstName": contact.first_name or "",
            "lastName": contact.last_name or "",
            "username": contact.username or "",
            "phone": str(contact.phone or ""),
            "email": contact.email or "",
            "club": contact.club or "",
            "role": contact.role or "",
        }
    return {
        "firstName": (user or {}).get("first_name") or "",
        "lastName": (user or {}).get("last_name") or "",
        "username": (user or {}).get("username") or "",
        "phone": "",
        "email": "",
        "club": "",
        "role": "",
    }


def get_missing_profile_fields(profile: dict[str, Any] | None) -> list[str]:
    miss: list[str] = []
    if not profile or not str(profile.get("firstName") or "").strip():
        miss.append("имя")
    if not profile or not profile.get("phone"):
        miss.append("телефон")
    if not profile or not str(profile.get("club") or "").strip():
        miss.append("клуб")
    if not profile or not profile.get("role"):
        miss.append("роль")
    return miss


def full_name_from_profile(profile: dict[str, Any]) -> str:
    return " ".join(
        filter(None, [profile.get("firstName"), profile.get("lastName")])
    ).strip()


def tg_ev_line(title: str, date_str: str | None) -> str:
    suffix = f" · {tg_date_fmt(date_str)[:5]}" if date_str else ""
    return f"<b>{tg_esc(title)}</b>{suffix}"


async def save_contact(
    session: AsyncSession,
    user: dict[str, Any],
    patch: dict[str, Any],
    trusted: bool = False,
) -> dict[str, str]:
    """
    Save or update a Contact row matching tgSaveContact_ in Code.gs.
    Phone is ONLY accepted when trusted=True (from verified Telegram contact message).
    """
    tg_id = str(user["id"])
    stmt = select(Contact).where(Contact.tg_id == tg_id).with_for_update()
    cur = (await session.execute(stmt)).scalar_one_or_none()

    clean: dict[str, str] = {}
    for k in ("firstName", "lastName", "email", "club"):
        if isinstance(patch, dict) and isinstance(patch.get(k), str):
            clean[k] = tg_cut(patch[k], 120)

    if clean.get("email") and not EMAIL_RE.match(clean["email"]):
        clean.pop("email", None)

    if isinstance(patch, dict) and patch.get("role") in TG_ROLES:
        clean["role"] = patch["role"]

    if trusted and isinstance(patch, dict) and patch.get("phone"):
        clean["phone"] = re.sub(r"[^\d+]", "", str(patch["phone"]))[:20]

    base = contact_to_profile(cur, user)
    merged = {**base, **clean, "username": user.get("username") or ""}
    now = datetime.now(timezone.utc)

    if cur is None:
        cur = Contact(
            tg_id=tg_id,
            first_name=merged["firstName"],
            last_name=merged["lastName"],
            username=merged["username"],
            phone=merged["phone"],
            email=merged["email"],
            club=merged["club"],
            role=merged["role"],
            updated_at=now,
        )
        session.add(cur)
    else:
        cur.first_name = merged["firstName"]
        cur.last_name = merged["lastName"]
        cur.username = merged["username"]
        cur.phone = merged["phone"]
        cur.email = merged["email"]
        cur.club = merged["club"]
        cur.role = merged["role"]
        cur.updated_at = now

    await session.flush()
    return merged


async def upsert_bot_user(
    session: AsyncSession,
    from_user: dict[str, Any],
    reminders_patch: bool | None = None,
) -> bool:
    """Upsert BotUser matching tgUser_ in Code.gs."""
    tg_id = str(from_user["id"])
    stmt = select(BotUser).where(BotUser.tg_id == tg_id).with_for_update()
    user_rec = (await session.execute(stmt)).scalar_one_or_none()
    if user_rec is not None:
        user_rec.name = tg_name(from_user)
        user_rec.username = from_user.get("username") or ""
        if reminders_patch is not None:
            user_rec.reminders = reminders_patch
        await session.flush()
        return bool(user_rec.reminders)

    reminders_val = True if reminders_patch is None else reminders_patch
    user_rec = BotUser(
        tg_id=tg_id,
        name=tg_name(from_user),
        username=from_user.get("username") or "",
        started_at=datetime.now(timezone.utc),
        reminders=reminders_val,
    )
    session.add(user_rec)
    await session.flush()
    return reminders_val


async def build_app_state(
    session: AsyncSession,
    user: dict[str, Any],
) -> dict[str, Any]:
    """Build Mini App state response matching tgAppState_ in Code.gs."""
    tg_id = str(user["id"])
    contact = (
        await session.execute(select(Contact).where(Contact.tg_id == tg_id))
    ).scalar_one_or_none()
    profile = contact_to_profile(contact, user)
    bot_username = await get_kv(session, "TG_BOT_USERNAME", "")

    rsvps_result = await session.execute(
        select(Rsvp).order_by(Rsvp.created_at.asc())
    )
    all_rsvps = rsvps_result.scalars().all()
    counts: dict[str, int] = {}
    mine: list[dict[str, Any]] = []

    for r in all_rsvps:
        if r.status != "going":
            continue
        counts[r.event_id] = counts.get(r.event_id, 0) + 1
        if r.tg_id == tg_id:
            mine.append(
                {
                    "eventId": r.event_id,
                    "title": r.title,
                    "date": r.date,
                    "code": r.code,
                    "qr": generate_qr_data_uri(r.code, bot_username),
                    "checkedIn": bool(r.checked_in_at),
                }
            )

    return {
        "ok": True,
        "profile": profile,
        "registered": contact is not None,
        "missing": get_missing_profile_fields(profile),
        "rsvps": mine,
        "counts": counts,
        "bot": bot_username,
    }


async def set_rsvp(
    session: AsyncSession,
    user: dict[str, Any],
    ev: Any,
    going: bool,
    send_qr_fn: Callable[..., Awaitable[Any]] | None = None,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> dict[str, Any]:
    """Create, reactivate, or cancel an RSVP matching tgRsvpSet_ in Code.gs."""
    if (
        not isinstance(ev, dict)
        or not isinstance(ev.get("id"), str)
        or not EVENT_ID_RE.match(ev["id"])
        or not ISO_DATE_RE.match(str(ev.get("date") or ""))
    ):
        return {"error": "bad_event"}

    tg_id = str(user["id"])
    contact = (
        await session.execute(select(Contact).where(Contact.tg_id == tg_id))
    ).scalar_one_or_none()
    profile = contact_to_profile(contact, user) if contact else None

    if going and (not profile or get_missing_profile_fields(profile)):
        return {"error": "profile"}

    if going and tg_days_from_today(str(ev["date"]), today_iso=today_iso) < 0:
        return {"error": "past"}

    stmt = (
        select(Rsvp)
        .where(Rsvp.event_id == ev["id"], Rsvp.tg_id == tg_id)
        .with_for_update()
    )
    hit = (await session.execute(stmt)).scalar_one_or_none()
    title = tg_cut(ev.get("title") or "Событие", 200)
    club = tg_cut(ev.get("club") or "", 200)
    name = full_name_from_profile(profile) if profile else tg_name(user)

    target_status = "going" if going else "cancelled"
    if hit is not None:
        if hit.status == target_status:
            return {}
        hit.status = target_status
        if going:
            hit.title = title
            hit.date = str(ev["date"])
            hit.club = club
        await session.flush()
    else:
        if not going:
            return {}
        code = secrets.token_hex(6)
        hit = Rsvp(
            code=code,
            event_id=ev["id"],
            title=title,
            date=str(ev["date"]),
            club=club,
            tg_id=tg_id,
            name=name,
            status="going",
            created_at=datetime.now(timezone.utc),
            checked_in_at=None,
            checked_by=None,
        )
        session.add(hit)
        await session.flush()

    bot_username = await get_kv(session, "TG_BOT_USERNAME", "")
    ev_line = tg_ev_line(hit.title, hit.date)
    if going:
        caption = f"✅ Вы зарегистрированы: {ev_line}\n\nПокажите этот QR на входе. Напомню за день до события."
        qr_sender = send_qr_fn or send_tg_qr_photo
        await qr_sender(tg_id, hit.code, caption, bot_username)
    else:
        msg_sender = send_msg_fn or send_tg_message
        await msg_sender(tg_id, f"Регистрация отменена: {ev_line}")

    return {}


async def handle_mini_app_api(
    session: AsyncSession,
    req: dict[str, Any],
    bot_token: str | None = None,
    send_qr_fn: Callable[..., Awaitable[Any]] | None = None,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> dict[str, Any]:
    """Handle Mini App API (me / reg / rsvp) matching tgAppApi_ in Code.gs."""
    body = req.get("me") or req.get("reg") or req.get("rsvp")
    init_data = body.get("initData") if isinstance(body, dict) else None
    user = verify_init_data(init_data, bot_token=bot_token)
    if not user:
        return {"ok": False, "error": "auth"}

    note: dict[str, Any] = {}
    if "reg" in req and isinstance(req["reg"], dict):
        await save_contact(
            session, user, req["reg"].get("profile") or {}, trusted=False
        )
    if "rsvp" in req and isinstance(req["rsvp"], dict):
        note = await set_rsvp(
            session,
            user,
            req["rsvp"].get("event"),
            bool(req["rsvp"].get("going")),
            send_qr_fn=send_qr_fn,
            send_msg_fn=send_msg_fn,
            today_iso=today_iso,
        )

    state = await build_app_state(session, user)
    if note.get("error"):
        state["ok"] = False
        state["error"] = note["error"]
    return state


# ===================== Check-in & CSV Export (§9.3, §9.4) =====================

async def can_manage_event(
    session: AsyncSession,
    user_id: int | str,
    contact: Contact | None,
    event_club: str,
) -> bool:
    if await is_tg_moderator(session, user_id):
        return True
    if contact is None or contact.role != "Организатор":
        return False
    c_club = (contact.club or "").strip().lower()
    r_club = (event_club or "").strip().lower()
    return bool(c_club and c_club in r_club)


def format_dt_almaty(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz()).strftime(fmt)


async def generate_event_csv(
    session: AsyncSession,
    event_id_prefix: str,
) -> dict[str, Any]:
    """Generate UTF-8 BOM CSV with semicolon delimiter and CRLF matching tgCsv_ in Code.gs."""
    contacts_res = await session.execute(select(Contact))
    people = {c.tg_id: c for c in contacts_res.scalars().all()}

    rsvps_res = await session.execute(
        select(Rsvp).order_by(Rsvp.created_at.asc())
    )
    rows = [
        r
        for r in rsvps_res.scalars().all()
        if r.event_id[:60] == event_id_prefix[:60]
    ]

    def quote_csv(val: Any) -> str:
        if isinstance(val, datetime):
            s = format_dt_almaty(val, "%d.%m.%Y %H:%M")
        else:
            s = "" if val is None else str(val)
        return '"' + s.replace('"', '""') + '"'

    headers = [
        "Имя",
        "Фамилия",
        "Telegram",
        "Телефон",
        "Email",
        "Клуб",
        "Роль",
        "Статус",
        "Записался",
        "Отмечен на входе",
    ]
    lines = [";".join(quote_csv(h) for h in headers)]

    for r in rows:
        p = people.get(r.tg_id)
        first_name = (p.first_name if p and p.first_name else r.name) or ""
        last_name = (p.last_name if p else "") or ""
        username = f"@{p.username}" if (p and p.username) else ""
        raw_phone = str(p.phone if p else "").lstrip("+")
        phone = f"+{raw_phone}" if raw_phone else ""
        email = (p.email if p else "") or ""
        club = (p.club if p else "") or ""
        role = (p.role if p else "") or ""
        status_label = "идёт" if r.status == "going" else "отменил"
        created_str = format_dt_almaty(r.created_at, "%d.%m.%Y %H:%M")
        checked_str = (
            format_dt_almaty(r.checked_in_at, "%d.%m.%Y %H:%M")
            if r.checked_in_at
            else ""
        )
        row_cells = [
            first_name,
            last_name,
            username,
            phone,
            email,
            club,
            role,
            status_label,
            created_str,
            checked_str,
        ]
        lines.append(";".join(quote_csv(c) for c in row_cells))

    going_count = sum(1 for r in rows if r.status == "going")
    return {
        "csv": "\ufeff" + "\r\n".join(lines),
        "first": rows[0] if rows else None,
        "count": going_count,
    }


async def handle_qr_checkin(
    session: AsyncSession,
    chat_id: int | str,
    from_user: dict[str, Any],
    code: str,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
) -> str:
    """Process QR check-in (/start ci_<code>) matching tgCheckIn_ in Code.gs."""
    sender = send_msg_fn or send_tg_message
    stmt = select(Rsvp).where(Rsvp.code == code).with_for_update()
    r = (await session.execute(stmt)).scalar_one_or_none()
    if r is None:
        text = "QR не найден. Возможно, регистрацию удалили."
        await sender(chat_id, text)
        return text

    scanner_id = str(from_user["id"])
    contact = (
        await session.execute(select(Contact).where(Contact.tg_id == scanner_id))
    ).scalar_one_or_none()

    ev_line = tg_ev_line(r.title, r.date)
    if not await can_manage_event(session, scanner_id, contact, r.club):
        if scanner_id == r.tg_id:
            text = f"Это ваш билет на {ev_line}. Покажите QR организатору на входе."
        else:
            text = "Отмечать участников могут организаторы клуба и штаб."
        await sender(chat_id, text)
        return text

    if r.status != "going":
        text = f"⚠️ {tg_esc(r.name)} отменил регистрацию на {ev_line}."
        await sender(chat_id, text)
        return text

    if r.checked_in_at is not None:
        hm = format_dt_almaty(r.checked_in_at, "%H:%M")
        text = f"Уже отмечен: {tg_esc(r.name)} · {hm}"
        await sender(chat_id, text)
        return text

    r.checked_in_at = datetime.now(timezone.utc)
    r.checked_by = tg_name(from_user)
    await session.flush()

    all_going_res = await session.execute(
        select(Rsvp).where(Rsvp.event_id == r.event_id, Rsvp.status == "going")
    )
    all_going = list(all_going_res.scalars().all())
    inside_count = sum(1 for x in all_going if x.checked_in_at is not None)

    text = f"✅ {tg_esc(r.name)} отмечен на входе · {ev_line}\nПришли {inside_count} из {len(all_going)}."
    if r.tg_id != scanner_id:
        await sender(
            r.tg_id,
            f"Вы отмечены на входе: {ev_line}. Хорошего события!",
        )
    await sender(chat_id, text)
    return text


# ===================== Portal Rows & Commands (§8.2) =====================

async def get_portal_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Read non-deleted portal rows and normalize legacy stage names matching tgPortalRows_."""
    res = await session.execute(
        select(Record).where(Record.tbl == "portal", Record.deleted.is_(False))
    )
    rows: list[dict[str, Any]] = []
    for rec in res.scalars().all():
        if not rec.row:
            continue
        raw_stage = rec.row.get("stage") or "Идея"
        norm_stage = TG_OLD_STAGE.get(raw_stage, raw_stage) or "Идея"
        rows.append({**rec.row, "stage": norm_stage})
    return rows


def format_ideas_text(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    if not rows:
        return (
            "В банке идей пока пусто. Подать первую: /new",
            tg_webapp_kb("Открыть портал"),
        )
    lines = ["<b>Банк идей</b>"]
    n = 0
    for st in IDEA_STAGE_ORDER:
        st_rows = [r for r in rows if r.get("stage") == st]
        if not st_rows or n >= 30:
            continue
        lines.append("")
        lines.append(f"<b>{st}</b> · {len(st_rows)}")
        for r in st_rows[: 30 - n]:
            n += 1
            title = tg_esc(r.get("title") or "Без названия")
            club_part = f" — {tg_esc(r['club'])}" if r.get("club") else ""
            date_part = (
                f" · {tg_date_fmt(r['date'])[:5]}" if r.get("date") else ""
            )
            lines.append(f"• {title}{club_part}{date_part}")
    return "\n".join(lines), tg_webapp_kb("Открыть банк идей", "tab=bank")


def format_calendar_text(
    rows: list[dict[str, Any]], today_iso: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    upcoming = [
        r
        for r in rows
        if r.get("date")
        and r.get("stage") not in ("Отклонена", "Закрыт")
        and tg_days_from_today(str(r["date"]), today_iso=today_iso) >= 0
    ]
    upcoming.sort(key=lambda r: str(r.get("date") or ""))
    if not upcoming:
        return (
            "Ближайших событий с датой пока нет.",
            tg_webapp_kb("Открыть календарь", "tab=cal"),
        )
    lines = ["<b>Ближайшие события</b>", ""]
    for r in upcoming[:15]:
        left = tg_days_from_today(str(r["date"]), today_iso=today_iso)
        day_tag = " · сегодня" if left == 0 else (" · завтра" if left == 1 else "")
        lines.append(
            f"<b>{tg_date_fmt(r['date'])[:5]}</b> · "
            f"{tg_esc(r.get('title') or 'Без названия')} · "
            f"{tg_esc(r.get('stage') or 'Идея')}{day_tag}"
        )
    return "\n".join(lines), tg_webapp_kb("Открыть календарь", "tab=cal")


async def get_managed_events(
    session: AsyncSession,
    from_user: dict[str, Any],
    today_iso: str | None = None,
) -> list[dict[str, Any]]:
    user_id = str(from_user["id"])
    contact = (
        await session.execute(select(Contact).where(Contact.tg_id == user_id))
    ).scalar_one_or_none()
    rsvps_res = await session.execute(
        select(Rsvp).order_by(Rsvp.created_at.asc())
    )
    by_id: dict[str, dict[str, Any]] = {}
    for r in rsvps_res.scalars().all():
        if r.date and tg_days_from_today(r.date, today_iso=today_iso) < -2:
            continue
        if not await can_manage_event(session, user_id, contact, r.club):
            continue
        entry = by_id.setdefault(
            r.event_id,
            {
                "id": r.event_id,
                "title": r.title,
                "date": r.date,
                "going": 0,
                "inside": 0,
            },
        )
        if r.status == "going":
            entry["going"] += 1
            if r.checked_in_at is not None:
                entry["inside"] += 1
    events = list(by_id.values())
    events.sort(key=lambda e: str(e.get("date") or ""))
    return events


async def submit_idea_from_bot(
    session: AsyncSession,
    from_user: dict[str, Any],
    row_fields: dict[str, Any],
    notify_mods_fn: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Create a portal card/proposal from the /new 5W1H dialog matching tgSubmitIdea_."""
    now_ms = int(time.time() * 1000)
    rand_suffix = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=3))
    base36_ts = ""
    n = now_ms
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    while n > 0:
        n, rem = divmod(n, 36)
        base36_ts = chars[rem] + base36_ts
    card_id = f"portal-tg{base36_ts}{rand_suffix}"

    contact_str = (
        f"@{from_user['username']}"
        if from_user.get("username")
        else tg_name(from_user)
    )
    full_row = {
        "_id": card_id,
        "_o": now_ms,
        "stage": "Идея",
        "contact": contact_str,
        **row_fields,
    }
    op = {
        "protocol": 4 if settings.is_moderated else 3,
        "table": "portal",
        "id": card_id,
        "mutationId": f"tg:{card_id}",
        "baseVersion": 0,
        "del": False,
        "row": full_row,
    }
    if settings.is_moderated:
        res = await propose_operation(
            session,
            op,
            bot_author=from_user,
            notify_mods_fn=notify_mods_fn or notify_moderators_on_propose,
        )
    else:
        res = await apply_operation(session, op)
    return {"res": res, "id": card_id}


# ===================== Dialogs, Messages, Callbacks & Webhook (§5.5, §8.2, §9.1) =====================

async def handle_reg_start(
    chat_id: int | str,
    from_user: dict[str, Any],
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    await state_store.remove(f"tgd{chat_id}")
    await state_store.put(
        f"tgr{chat_id}", json.dumps({"step": "name", "p": {}}), ttl=21600
    )
    guess = " ".join(
        filter(None, [from_user.get("first_name"), from_user.get("last_name")])
    ).strip()
    kb = (
        {
            "keyboard": [[{"text": guess}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
        if guess
        else None
    )
    return await sender(
        chat_id,
        "<b>Регистрация в Sunday Open</b>\nПять коротких шагов: имя, телефон, email, клуб, роль. Прервать: /cancel\n\n<b>1/5.</b> Как вас зовут? Имя и фамилия.",
        kb,
    )


async def handle_reg_step(
    chat_id: int | str,
    from_user: dict[str, Any],
    d: dict[str, Any],
    text: str,
    phone_saved: bool = False,
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    rkey = f"tgr{chat_id}"
    phone_kb = {
        "keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    step = d.get("step")

    if step == "name":
        parts = [p for p in text.split() if p]
        if not parts:
            return await sender(chat_id, "Напишите имя и фамилию.")
        d["p"]["firstName"] = tg_cut(parts[0], 60)
        d["p"]["lastName"] = tg_cut(" ".join(parts[1:]), 80)
        d["step"] = "phone"
        await state_store.put(rkey, json.dumps(d), ttl=21600)
        return await sender(
            chat_id,
            "<b>2/5.</b> Поделитесь номером телефона кнопкой ниже. Номер увидят только штаб и организаторы событий, на которые вы запишетесь.",
            phone_kb,
        )

    if step == "phone":
        if not phone_saved:
            return await sender(
                chat_id,
                "Нажмите кнопку «📱 Поделиться номером» под полем ввода.",
                phone_kb,
            )
        d["step"] = "email"
        await state_store.put(rkey, json.dumps(d), ttl=21600)
        return await sender(
            chat_id,
            "<b>3/5.</b> Email для связи. Если не хотите указывать, отправьте «-».",
            {"remove_keyboard": True},
        )

    if step == "email":
        if text not in ("-", "—"):
            if not EMAIL_RE.match(text):
                return await sender(
                    chat_id,
                    "Похоже, это не email. Попробуйте ещё раз или отправьте «-».",
                )
            d["p"]["email"] = tg_cut(text, 120)
        d["step"] = "club"
        await state_store.put(rkey, json.dumps(d), ttl=21600)
        return await sender(
            chat_id,
            "<b>4/5.</b> Какой клуб вы представляете? Напишите название, например AITU Gaming.",
        )

    if step == "club":
        if not text or text == "-":
            return await sender(chat_id, "Напишите название клуба.")
        d["p"]["club"] = tg_cut(text, 120)
        d["step"] = "role"
        await state_store.put(rkey, json.dumps(d), ttl=21600)
        role_kb = {
            "inline_keyboard": [
                [
                    {"text": role_name, "callback_data": f"g:{idx}"}
                    for idx, role_name in enumerate(TG_ROLES)
                ]
            ]
        }
        return await sender(chat_id, "<b>5/5.</b> Ваша роль в клубе?", role_kb)

    return await sender(chat_id, "Выберите роль кнопкой выше.")


async def handle_on_contact(
    session: AsyncSession,
    msg: dict[str, Any],
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    chat_id = msg["chat"]["id"]
    from_user = msg["from"]
    ct = msg["contact"]

    if not ct.get("user_id") or str(ct.get("user_id")) != str(from_user["id"]):
        return await sender(
            chat_id,
            "Нужен ваш собственный номер. Нажмите кнопку «Поделиться номером».",
        )

    await upsert_bot_user(session, from_user)
    profile = await save_contact(
        session,
        from_user,
        {"phone": ct.get("phone_number") or ""},
        trusted=True,
    )

    rkey = f"tgr{chat_id}"
    raw_d = await state_store.get(rkey)
    if raw_d:
        d = json.loads(raw_d)
        if d.get("step") == "phone":
            return await handle_reg_step(
                chat_id,
                from_user,
                d,
                "",
                phone_saved=True,
                state_store=state_store,
                send_msg_fn=sender,
            )

    miss = get_missing_profile_fields(profile)
    suffix = (
        f" Осталось заполнить: {', '.join(miss)}. Это можно сделать в портале или командой /register."
        if miss
        else ""
    )
    return await sender(
        chat_id,
        f"Номер сохранён.{suffix}",
        {"remove_keyboard": True},
    )


async def handle_new_dialog_step(
    chat_id: int | str,
    from_user: dict[str, Any],
    d: dict[str, Any],
    text: str,
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    dkey = f"tgd{chat_id}"
    step_idx = int(d.get("step", 0))
    field_name = TG_STEPS[step_idx][0]
    skip = text in ("-", "—")

    if field_name == "title" and skip:
        return await sender(
            chat_id, f"Без названия не получится. {TG_STEPS[0][1]}"
        )

    if field_name == "date" and not skip:
        iso = tg_parse_date(text, today_iso=today_iso)
        if not iso:
            return await sender(
                chat_id, "Не разобрал дату. Напишите, например, 15.11 или «-»."
            )
        d["row"]["date"] = iso
        left = tg_days_from_today(iso, today_iso=today_iso)
        if 0 <= left < 28:
            await sender(
                chat_id,
                "До события меньше 4 недель. Штаб принимает идеи заранее, поэтому может предложить другую дату.",
            )
    elif not skip:
        d["row"][field_name] = tg_cut(text, 2000)

    d["step"] = step_idx + 1
    if d["step"] < len(TG_STEPS):
        await state_store.put(dkey, json.dumps(d), ttl=21600)
        next_idx = d["step"]
        return await sender(
            chat_id,
            f"<b>{next_idx + 1}/{len(TG_STEPS)}.</b> {TG_STEPS[next_idx][1]}",
        )

    await state_store.put(dkey, json.dumps(d), ttl=21600)
    lines = ["<b>Проверьте заявку</b>", ""]
    for s_key, _ in TG_STEPS:
        val = d["row"].get(s_key)
        if not val:
            continue
        display_val = tg_date_fmt(val) if s_key == "date" else val
        lines.append(
            f"<b>{tg_esc(TG_LABELS.get(s_key, s_key))}:</b> {tg_esc(display_val)}"
        )

    kb = {
        "inline_keyboard": [
            [
                {"text": "Отправить в штаб", "callback_data": "n:send"},
                {"text": "Отменить", "callback_data": "n:cancel"},
            ]
        ]
    }
    return await sender(chat_id, "\n".join(lines), kb)


async def handle_tg_message(
    session: AsyncSession,
    msg: dict[str, Any],
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    chat_id = msg["chat"]["id"]
    from_user = msg["from"]
    text = str(msg.get("text") or "").strip()

    if msg.get("contact"):
        return await handle_on_contact(
            session, msg, state_store=state_store, send_msg_fn=sender
        )

    m = CMD_RE.match(text)
    cmd_name = m.group(1).lower() if m and m.group(1) else ""
    cmd_arg = m.group(2).strip() if m and m.group(2) else ""
    dkey = f"tgd{chat_id}"
    rkey = f"tgr{chat_id}"

    if cmd_name == "start" and CHECKIN_ARG_RE.match(cmd_arg):
        return await handle_qr_checkin(
            session, chat_id, from_user, cmd_arg[3:], send_msg_fn=sender
        )

    if (cmd_name == "start" and cmd_arg == "reg") or cmd_name == "register":
        return await handle_reg_start(
            chat_id, from_user, state_store=state_store, send_msg_fn=sender
        )

    if cmd_name == "list":
        evs = await get_managed_events(session, from_user, today_iso=today_iso)
        if not evs:
            return await sender(
                chat_id,
                "Списков пока нет. Команда работает для штаба и для организаторов клуба (роль «Организатор» в /register), когда на события клуба кто-то записался.",
            )
        inline_rows = [
            [
                {
                    "text": f"{tg_date_fmt(e['date'])[:5]} · {tg_cut(e['title'], 34)} · {e['going']}"
                    + (f" (пришли {e['inside']})" if e["inside"] else ""),
                    "callback_data": f"l:{e['id'][:60]}",
                }
            ]
            for e in evs[:20]
        ]
        return await sender(
            chat_id,
            "<b>Списки участников</b>\nНажмите на событие, пришлю таблицу CSV (открывается в Excel и Google Таблицах).",
            {"inline_keyboard": inline_rows},
        )

    rdlg = await state_store.get(rkey)
    if rdlg and not cmd_name:
        return await handle_reg_step(
            chat_id,
            from_user,
            json.loads(rdlg),
            text,
            state_store=state_store,
            send_msg_fn=sender,
        )

    if cmd_name == "menu":
        from backend.anchor_ui import create_default_navigator

        nav = create_default_navigator(state_store=state_store)
        await upsert_bot_user(session, from_user)
        await nav.reset_history(from_user["id"], session=session)
        return await nav.navigate(
            session=session,
            user_id=from_user["id"],
            chat_id=chat_id,
            screen_id="home",
            payload={},
            push_to_history=True,
            force_new_message=True,
            send_msg_fn=sender,
        )

    if cmd_name in ("start", "help"):
        await upsert_bot_user(session, from_user)
        help_text = (
            "<b>Sunday Open · Проекты</b>\n\n"
            "Здесь клубы подают идеи, заполняют паспорт проекта и смотрят календарь. "
            "Правки сначала смотрит штаб, я напишу, когда их одобрят.\n\n"
            "/register — регистрация клуба и контакты\n"
            "/new — подать идею прямо в чате\n"
            "/ideas — банк идей\n"
            "/calendar — ближайшие события\n"
            "/remind — включить или выключить напоминания\n"
            "/list — списки участников (организаторам)\n"
            "/cancel — прервать подачу идеи или регистрацию"
        )
        return await sender(chat_id, help_text, tg_webapp_kb("Открыть портал"))

    if cmd_name == "cancel":
        await state_store.remove(dkey)
        await state_store.remove(rkey)
        return await sender(
            chat_id,
            "Прервал. Подать идею: /new, регистрация: /register",
            {"remove_keyboard": True},
        )

    if cmd_name == "mod":
        mod_code = await get_kv(session, "TG_MOD_CODE", "")
        if not cmd_arg or not mod_code or cmd_arg != mod_code:
            return await sender(
                chat_id,
                "Код не подошёл. Код модератора есть в журнале выполнения setupSundayBot.",
            )
        mods = [
            m
            for m in await get_tg_moderators(session)
            if str(m.get("id")) != str(from_user["id"])
        ]
        mods.append({"id": from_user["id"], "name": tg_name(from_user)})
        await set_kv(
            session, "TG_MODERATORS", json.dumps(mods, ensure_ascii=False)
        )
        await upsert_bot_user(session, from_user)
        return await sender(
            chat_id,
            "Готово, вы модератор. Новые правки будут приходить сюда с кнопками «Одобрить» и «Отклонить». Отключиться: /unmod",
        )

    if cmd_name == "unmod":
        mods = [
            m
            for m in await get_tg_moderators(session)
            if str(m.get("id")) != str(from_user["id"])
        ]
        await set_kv(
            session, "TG_MODERATORS", json.dumps(mods, ensure_ascii=False)
        )
        return await sender(chat_id, "Уведомления модератора отключены.")

    if cmd_name == "remind":
        cur_rem = await upsert_bot_user(session, from_user)
        next_rem = not cur_rem
        await upsert_bot_user(session, from_user, reminders_patch=next_rem)
        msg_text = (
            "Напоминания включены: за неделю и за день до событий, плюс дедлайны по вашим идеям."
            if next_rem
            else "Напоминания выключены. Включить снова: /remind"
        )
        return await sender(chat_id, msg_text)

    if cmd_name in ("ideas", "calendar"):
        rows = await get_portal_rows(session)
        if cmd_name == "ideas":
            txt, kb = format_ideas_text(rows)
        else:
            txt, kb = format_calendar_text(rows, today_iso=today_iso)
        return await sender(chat_id, txt, kb)

    if cmd_name == "new":
        await state_store.put(
            dkey, json.dumps({"step": 0, "row": {}}), ttl=21600
        )
        return await sender(
            chat_id,
            f"Подаём идею по 5W1H, {len(TG_STEPS)} коротких вопросов. Пропустить вопрос: «-». Прервать: /cancel\n\n<b>1/{len(TG_STEPS)}.</b> {TG_STEPS[0][1]}",
        )

    d_raw = await state_store.get(dkey)
    if d_raw and not cmd_name:
        return await handle_new_dialog_step(
            chat_id,
            from_user,
            json.loads(d_raw),
            text,
            state_store=state_store,
            send_msg_fn=sender,
            today_iso=today_iso,
        )

    return await sender(
        chat_id,
        "Не понял. Подать идею: /new, список команд: /help",
        tg_webapp_kb("Открыть портал"),
    )


async def handle_tg_callback(
    session: AsyncSession,
    cb: dict[str, Any],
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    send_api_fn: Callable[..., Awaitable[Any]] | None = None,
    send_doc_fn: Callable[..., Awaitable[Any]] | None = None,
) -> Any:
    sender = send_msg_fn or send_tg_message
    api_caller = send_api_fn or send_tg_api
    doc_sender = send_doc_fn or send_tg_csv_document

    data = str(cb.get("data") or "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    from_user = cb.get("from") or {}

    async def answer(text_str: str = "", alert: bool = False) -> Any:
        return await api_caller(
            "answerCallbackQuery",
            {
                "callback_query_id": cb.get("id"),
                "text": text_str,
                "show_alert": bool(alert),
            },
        )

    async def close_msg(suffix: str) -> Any:
        orig_text = msg.get("text") or ""
        return await api_caller(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"{orig_text}\n\n{suffix}",
            },
        )

    # Anchor UI LIFO navigation callbacks (nav:back, nav:home, nav:open:*, nav:stage:*, nav:card:*)
    if data.startswith("nav:"):
        from backend.anchor_ui import create_default_navigator

        nav = create_default_navigator(state_store=state_store)
        uid = from_user.get("id")
        if data == "nav:back":
            await nav.back(
                session=session,
                user_id=uid,
                chat_id=chat_id,
                anchor_message_id=message_id,
                send_msg_fn=sender,
                send_api_fn=api_caller,
            )
            return await answer()
        if data == "nav:home":
            await nav.reset_history(uid, session=session)
            await nav.navigate(
                session=session,
                user_id=uid,
                chat_id=chat_id,
                screen_id="home",
                payload={},
                push_to_history=True,
                anchor_message_id=message_id,
                send_msg_fn=sender,
                send_api_fn=api_caller,
            )
            return await answer()
        if data.startswith("nav:open:"):
            target_screen = data[len("nav:open:") :]
            await nav.navigate(
                session=session,
                user_id=uid,
                chat_id=chat_id,
                screen_id=target_screen,
                payload={},
                push_to_history=True,
                anchor_message_id=message_id,
                send_msg_fn=sender,
                send_api_fn=api_caller,
            )
            return await answer()
        if data.startswith("nav:stage:"):
            stage_name = data[len("nav:stage:") :]
            await nav.navigate(
                session=session,
                user_id=uid,
                chat_id=chat_id,
                screen_id="ideas_stage",
                payload={"stage": stage_name},
                push_to_history=True,
                anchor_message_id=message_id,
                send_msg_fn=sender,
                send_api_fn=api_caller,
            )
            return await answer()
        if data.startswith("nav:card:"):
            card_id = data[len("nav:card:") :]
            await nav.navigate(
                session=session,
                user_id=uid,
                chat_id=chat_id,
                screen_id="idea_detail",
                payload={"card_id": card_id},
                push_to_history=True,
                anchor_message_id=message_id,
                send_msg_fn=sender,
                send_api_fn=api_caller,
            )
            return await answer()

    # Role selection in /register (g:0, g:1, g:2)
    if re.match(r"^g:\d$", data):
        role_idx = int(data[2:])
        rkey = f"tgr{chat_id}"
        raw_d = await state_store.get(rkey)
        if not raw_d:
            return await answer("Регистрация устарела. Начните заново: /register", True)
        d = json.loads(raw_d)
        role_name = TG_ROLES[role_idx]
        d["p"]["role"] = role_name
        await upsert_bot_user(session, from_user)
        p = await save_contact(session, from_user, d["p"], trusted=False)
        await state_store.remove(rkey)
        await close_msg(f"Роль: {role_name}")
        miss = get_missing_profile_fields(p)
        prefix = (
            f"Сохранил. Не хватает: {', '.join(miss)}."
            if miss
            else "✅ Готово, вы зарегистрированы."
        )
        org_note = (
            "\nКак организатор вы получите список участников событий своего клуба за день до события, а также по команде /list."
            if p.get("role") == "Организатор"
            else ""
        )
        await sender(
            chat_id,
            f"{prefix}\n\n"
            f"{tg_esc(full_name_from_profile(p))} · {tg_esc(p.get('club') or '')} · {p.get('role')}\n\n"
            f"Теперь можно записываться на события в портале: вкладка «План», кнопка «Зарегистрироваться».{org_note}",
            tg_webapp_kb("Открыть план", "tab=plan"),
        )
        return await answer("Сохранено")

    # CSV export button in /list (l:<eventId>)
    if data.startswith("l:"):
        event_id = data[2:]
        user_id = str(from_user["id"])
        contact = (
            await session.execute(select(Contact).where(Contact.tg_id == user_id))
        ).scalar_one_or_none()
        csv_data = await generate_event_csv(session, event_id)
        first_row = csv_data["first"]
        if not first_row or not await can_manage_event(
            session, user_id, contact, first_row.club
        ):
            return await answer(
                "Список доступен штабу и организаторам клуба.", True
            )
        safe_title = re.sub(r'[\\/:*?"<>|]', "", tg_cut(first_row.title, 40))
        filename = f"участники {tg_date_fmt(first_row.date)[:5]} {safe_title}.csv"
        caption = f"{tg_ev_line(first_row.title, first_row.date)}\nИдут: {csv_data['count']}"
        await doc_sender(chat_id, filename, csv_data["csv"], caption)
        return await answer("Отправил")

    # /new dialog send or cancel (n:send, n:cancel)
    if data in ("n:cancel", "n:send"):
        dkey = f"tgd{chat_id}"
        raw_d = await state_store.get(dkey)
        if not raw_d:
            return await answer("Заявка устарела. Начните заново: /new", True)
        await state_store.remove(dkey)
        if data == "n:cancel":
            await close_msg("Отменено.")
            return await answer()
        d = json.loads(raw_d)
        out = await submit_idea_from_bot(session, from_user, d.get("row") or {})
        res = out["res"]
        if res.get("ok") is False and res.get("status") != "pending":
            await state_store.put(dkey, raw_d, ttl=21600)
            return await answer("Таблица занята, нажмите ещё раз через минуту.", True)
        status_note = (
            "Отправлено в штаб. Напишу, когда решат."
            if res.get("status") == "pending"
            else "Опубликовано."
        )
        await close_msg(status_note)
        await sender(
            chat_id,
            "Карточку можно дополнить в портале: паспорт, контакты, детали.",
            tg_webapp_kb("Открыть карточку", f"sel={quote(out['id'])}"),
        )
        return await answer("Отправлено")

    # Moderator approve / reject (a:<proposalRef>:<hash10> or r:<proposalRef>:<hash10>)
    m = MOD_CB_RE.match(data)
    if not m:
        return await answer()

    action_char, proposal_ref, hash_prefix = m.group(1), m.group(2), m.group(3)
    if not await is_tg_moderator(session, from_user["id"]):
        return await answer(
            "Одобрять правки могут только модераторы штаба.", True
        )

    try:
        reviewer_str = f"tg:{from_user['id']} {tg_name(from_user)}"
        result = await review_proposal(
            session=session,
            proposal_ref=proposal_ref,
            approve=(action_char == "a"),
            reviewer=reviewer_str,
            hash_prefix=hash_prefix,
            send_author_fn=sender,
        )
    except LookupError:
        return await answer(
            "Правка не найдена или таблица занята. Попробуйте ещё раз.", True
        )

    status_labels = {
        "approved": "✅ Одобрено",
        "rejected": "❌ Отклонено",
        "conflict": "⚠️ Конфликт: карточку уже изменили, автор отправит заново",
    }
    label = status_labels.get(result["status"], result["status"])
    suffix_actor = (
        f" · {tg_name(from_user)}" if result["changed"] else " · рассмотрено раньше"
    )
    await close_msg(f"{label}{suffix_actor}")
    return await answer(label)


async def handle_tg_webhook(
    session: AsyncSession,
    secret_param: str | None,
    header_secret: str | None,
    update: Any,
    state_store: StateStore = default_state_store,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    send_api_fn: Callable[..., Awaitable[Any]] | None = None,
    send_doc_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> bool:
    """
    Validate webhook secret, deduplicate update_id for 6 hours, and dispatch message or callback_query.
    Matches tgWebhook_ in Code.gs and §5.5.
    """
    if not isinstance(update, dict):
        return False

    expected_secret = await get_kv(session, "TG_SECRET", "")
    if not expected_secret:
        return False

    if secret_param != expected_secret:
        return False
    if header_secret is not None and header_secret != expected_secret:
        return False

    update_id = update.get("update_id")
    if update_id is not None:
        dedup_key = f"tgu{update_id}"
        if await state_store.get(dedup_key):
            return False
        await state_store.put(dedup_key, "1", ttl=21600)

    if "callback_query" in update and isinstance(update["callback_query"], dict):
        await handle_tg_callback(
            session,
            update["callback_query"],
            state_store=state_store,
            send_msg_fn=send_msg_fn,
            send_api_fn=send_api_fn,
            send_doc_fn=send_doc_fn,
        )
        return True

    msg = update.get("message")
    if (
        not isinstance(msg, dict)
        or not msg.get("from")
        or not isinstance(msg.get("chat"), dict)
        or msg["chat"].get("type") != "private"
        or (not isinstance(msg.get("text"), str) and not msg.get("contact"))
    ):
        return False

    await handle_tg_message(
        session,
        msg,
        state_store=state_store,
        send_msg_fn=send_msg_fn,
        today_iso=today_iso,
    )
    return True


# ===================== Daily 09:00 Scheduled Job (§11 & sundayBotDaily) =====================

async def run_rsvp_daily(
    session: AsyncSession,
    send_qr_fn: Callable[..., Awaitable[Any]] | None = None,
    send_doc_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
) -> None:
    """Send tomorrow's RSVP QR reminders to attendees and CSV lists to moderators & club organisers."""
    qr_sender = send_qr_fn or send_tg_qr_photo
    doc_sender = send_doc_fn or send_tg_csv_document

    bot_username = await get_kv(session, "TG_BOT_USERNAME", "")
    rsvps_res = await session.execute(
        select(Rsvp).where(Rsvp.status == "going").order_by(Rsvp.created_at.asc())
    )
    tomorrow_rsvps = [
        r
        for r in rsvps_res.scalars().all()
        if r.date and tg_days_from_today(r.date, today_iso=today_iso) == 1
    ]
    if not tomorrow_rsvps:
        return

    for r in tomorrow_rsvps:
        ev_line = tg_ev_line(r.title, r.date)
        caption = (
            f"⏰ Завтра: {ev_line}\n"
            "Ваш QR для входа. Если не сможете прийти, отмените регистрацию в портале, чтобы освободить место."
        )
        await qr_sender(r.tg_id, r.code, caption, bot_username)

    events_by_id: dict[str, Rsvp] = {}
    for r in tomorrow_rsvps:
        events_by_id[r.event_id] = r

    contacts_res = await session.execute(select(Contact))
    all_contacts = list(contacts_res.scalars().all())
    mods = await get_tg_moderators(session)

    for event_id, r in events_by_id.items():
        csv_data = await generate_event_csv(session, event_id)
        recipients: set[str] = {str(m["id"]) for m in mods if m.get("id")}
        r_club_low = (r.club or "").strip().lower()
        for c in all_contacts:
            c_club_low = (c.club or "").strip().lower()
            if (
                c.role == "Организатор"
                and c_club_low
                and c_club_low in r_club_low
            ):
                recipients.add(str(c.tg_id))

        filename = f"участники {tg_date_fmt(r.date)[:5]}.csv"
        caption = (
            f"Завтра {tg_ev_line(r.title, r.date)}\n"
            f"Зарегистрировано: {csv_data['count']}. "
            "Сканируйте QR участников камерой телефона — бот отметит вход."
        )
        for tid in recipients:
            await doc_sender(tid, filename, csv_data["csv"], caption)


async def run_sunday_bot_daily(
    session: AsyncSession,
    send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
    send_qr_fn: Callable[..., Awaitable[Any]] | None = None,
    send_doc_fn: Callable[..., Awaitable[Any]] | None = None,
    today_iso: str | None = None,
    now_dt: datetime | None = None,
) -> str:
    """Execute daily 09:00 notifications matching sundayBotDaily in Code.gs and §11."""
    sender = send_msg_fn or send_tg_message
    current_dt = now_dt or datetime.now(timezone.utc)

    try:
        await run_rsvp_daily(
            session,
            send_qr_fn=send_qr_fn,
            send_doc_fn=send_doc_fn,
            today_iso=today_iso,
        )
    except Exception:
        pass

    rows = await get_portal_rows(session)
    users_res = await session.execute(select(BotUser))
    users = list(users_res.scalars().all())

    authors_res = await session.execute(
        select(Author).where(Author.tbl == "portal")
    )
    owners: dict[str, str] = {
        a.row_id: a.tg_id for a in authors_res.scalars().all()
    }

    props_res = await session.execute(
        select(Proposal).where(Proposal.status == "pending")
    )
    pending_old = 0
    for p in props_res.scalars().all():
        created = (
            p.created_at
            if p.created_at.tzinfo
            else p.created_at.replace(tzinfo=timezone.utc)
        )
        if (current_dt - created).total_seconds() > 86400:
            pending_old += 1

    # 1. Subscribers with reminders=True: events 7 days and 1 day away
    soon: list[str] = []
    for r in rows:
        date_val = r.get("date")
        if not date_val or r.get("stage") not in TG_LIVE:
            continue
        d = tg_days_from_today(str(date_val), today_iso=today_iso)
        if d == 7:
            soon.append(
                f"Через неделю, {tg_date_fmt(date_val)[:5]}: <b>{tg_esc(r.get('title'))}</b>"
            )
        elif d == 1:
            soon.append(f"Завтра: <b>{tg_esc(r.get('title'))}</b>")

    if soon:
        soon_text = "<b>Sunday Open</b>\n" + "\n".join(soon)
        cal_kb = tg_webapp_kb("Календарь", "tab=cal")
        for u in users:
            if u.reminders:
                await sender(u.tg_id, soon_text, cal_kb)

    # 2. Authors per card: 31 days (stage Идея) and 14 days (stage Одобрена/Паспорт)
    personal: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        card_id = str(r.get("_id") or "")
        who = owners.get(card_id)
        date_val = r.get("date")
        if not who or not date_val:
            continue
        d = tg_days_from_today(str(date_val), today_iso=today_iso)
        t_bold = f"<b>{tg_esc(r.get('title'))}</b>"
        line: str | None = None
        if r.get("stage") in TG_EARLY and d - 28 == 3:
            line = f"Через 3 дня закрывается приём идеи {t_bold} (за 4 недели до события). Проверьте, что заявка заполнена."
        elif r.get("stage") in ("Одобрена", "Паспорт") and d == 14:
            line = f"До {t_bold} две недели, а паспорт проекта ещё не отправлен на проверку."
        if line:
            personal.setdefault(who, []).append({"text": line, "id": card_id})

    for who, alerts in personal.items():
        for alert in alerts:
            await sender(
                who,
                alert["text"],
                tg_webapp_kb("Открыть карточку", f"sel={quote(alert['id'])}"),
            )

    # 3. Moderators: proposals pending >24h + stage-Идея cards 0..27 days away
    late = [
        r
        for r in rows
        if r.get("date")
        and r.get("stage") in TG_EARLY
        and 0 <= tg_days_from_today(str(r["date"]), today_iso=today_iso) < 28
    ]
    if late or pending_old:
        mod_lines = ["<b>Штабу на сегодня</b>"]
        if pending_old:
            mod_lines.append(f"Правок ждут решения больше суток: {pending_old}")
        if late:
            mod_lines.append("")
            mod_lines.append("Идеи без решения, до события меньше 4 недель:")
            for r in late[:15]:
                mod_lines.append(
                    f"• {tg_esc(r.get('title'))} · {tg_date_fmt(r['date'])[:5]} · {tg_esc(r.get('stage'))}"
                )
        mods = await get_tg_moderators(session)
        portal_kb = tg_webapp_kb("Открыть портал")
        mod_text = "\n".join(mod_lines)
        for m in mods:
            if m.get("id"):
                await sender(m["id"], mod_text, portal_kb)

    return "Done"
