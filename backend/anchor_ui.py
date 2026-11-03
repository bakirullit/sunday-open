from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
from typing import Any
from urllib.parse import quote

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from backend.bot import (
    IDEA_STAGE_ORDER,
    StateStore,
    contact_to_profile,
    default_state_store,
    format_calendar_text,
    format_ideas_text,
    full_name_from_profile,
    get_managed_events,
    get_missing_profile_fields,
    get_portal_rows,
)
from backend.models import Contact
from backend.moderation import (
    TG_LABELS,
    get_kv,
    send_tg_api,
    send_tg_message,
     set_kv,
    tg_cut,
    tg_date_fmt,
    tg_esc,
    tg_webapp_button,
    tg_webapp_kb,
)
from sqlalchemy import select

logger = logging.getLogger("backend.anchor_ui")


@dataclass
class Screen:
    text: str
    reply_markup: dict[str, Any] | None = None
    parse_mode: str = "HTML"
    title: str = ""
    breadcrumbs: list[str] = field(default_factory=list)


ScreenRenderer = Callable[
    [AsyncSession, int | str, int | str, dict[str, Any]], Awaitable[Screen]
]


class NavigationStackManager:
    """
    Manages user screen navigation history as a LIFO stack in Redis/StateStore
    with optional PostgreSQL kv persistence fallback.
    """

    def __init__(
        self,
        state_store: StateStore = default_state_store,
        redis_client: Redis | None = None,
        max_depth: int = 20,
    ) -> None:
        self._store = state_store
        self._redis = redis_client
        self._max_depth = max_depth

    def _key(self, user_id: int | str) -> str:
        return f"anchor:user:{user_id}:stack"

    async def _load_stack(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> list[dict[str, Any]]:
        key = self._key(user_id)
        if self._redis is not None:
            try:
                raw_items = await self._redis.lrange(key, 0, self._max_depth - 1)
                if raw_items:
                    return [
                        json.loads(x.decode("utf-8") if isinstance(x, bytes) else x)
                        for x in raw_items
                    ]
            except Exception:
                pass
        raw = await self._store.get(key)
        if not raw and session is not None:
            raw = await get_kv(session, key, "")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    async def _save_stack(
        self,
        user_id: int | str,
        stack: list[dict[str, Any]],
        session: AsyncSession | None = None,
    ) -> None:
        key = self._key(user_id)
        trimmed = stack[: self._max_depth]
        raw = json.dumps(trimmed, ensure_ascii=False)
        await self._store.put(key, raw, ttl=86400)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
                if trimmed:
                    for item in reversed(trimmed):
                        await self._redis.lpush(
                            key, json.dumps(item, ensure_ascii=False)
                        )
                    await self._redis.expire(key, 86400)
            except Exception:
                pass
        if session is not None:
            await set_kv(session, key, raw)

    async def push(
        self,
        user_id: int | str,
        screen_id: str,
        payload: dict[str, Any] | None = None,
        title: str = "",
        session: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        """Push a screen onto the top (index 0) of the LIFO stack."""
        stack = await self._load_stack(user_id, session=session)
        entry = {
            "screen_id": screen_id,
            "payload": payload or {},
            "title": title or screen_id,
        }
        # Avoid duplicate consecutive entries on top of stack
        if stack and (
            stack[0].get("screen_id") == screen_id
            and stack[0].get("payload") == entry["payload"]
        ):
            stack[0] = entry
        else:
            stack.insert(0, entry)
        stack = stack[: self._max_depth]
        await self._save_stack(user_id, stack, session=session)
        return stack

    async def pop(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        """Pop the active screen off the top of the LIFO stack."""
        stack = await self._load_stack(user_id, session=session)
        if not stack:
            return None
        top = stack.pop(0)
        await self._save_stack(user_id, stack, session=session)
        return top

    async def peek(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        """Inspect the active screen at the top of the LIFO stack without removing it."""
        stack = await self._load_stack(user_id, session=session)
        return stack[0] if stack else None

    async def depth(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> int:
        stack = await self._load_stack(user_id, session=session)
        return len(stack)

    async def breadcrumbs(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> list[str]:
        """Return breadcrumb titles ordered from root (oldest) to current (top)."""
        stack = await self._load_stack(user_id, session=session)
        return [
            str(item.get("title") or item.get("screen_id"))
            for item in reversed(stack)
        ]

    async def clear(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> None:
        """Reset navigation history for the user."""
        key = self._key(user_id)
        await self._store.remove(key)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception:
                pass
        if session is not None:
            await set_kv(session, key, "[]")


class AnchorNavigator:
    """
    Anchor UI Engine managing a single persistent interactive message per user
    with a LIFO navigation stack and breadcrumbs.
    """

    def __init__(
        self,
        state_store: StateStore = default_state_store,
        redis_client: Redis | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._store = state_store
        self._redis = redis_client
        self._bot = bot
        self.stack = NavigationStackManager(
            state_store=state_store, redis_client=redis_client
        )
        self._renderers: dict[str, tuple[str, ScreenRenderer]] = {}

    def _anchor_key(self, user_id: int | str) -> str:
        return f"anchor:user:{user_id}:message_id"

    def register_screen(
        self, screen_id: str, title: str, renderer: ScreenRenderer
    ) -> None:
        self._renderers[screen_id] = (title, renderer)

    async def get_anchor_id(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> int | None:
        key = self._anchor_key(user_id)
        val = await self._store.get(key)
        if not val and session is not None:
            val = await get_kv(session, key, "")
        if not val:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    async def set_anchor_id(
        self,
        user_id: int | str,
        message_id: int,
        session: AsyncSession | None = None,
    ) -> None:
        key = self._anchor_key(user_id)
        await self._store.put(key, str(message_id), ttl=2592000)
        if session is not None:
            await set_kv(session, key, str(message_id))

    async def reset_history(
        self, user_id: int | str, session: AsyncSession | None = None
    ) -> None:
        await self.stack.clear(user_id, session=session)

    @staticmethod
    def _with_navigation_buttons(
        markup: dict[str, Any] | None,
        stack_depth: int,
    ) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        if markup and isinstance(markup.get("inline_keyboard"), list):
            rows = [list(r) for r in markup["inline_keyboard"]]

        if stack_depth > 1:
            has_back = any(
                btn.get("callback_data") == "nav:back"
                for row in rows
                for btn in row
            )
            if not has_back:
                nav_row = [{"text": "⬅️ Назад", "callback_data": "nav:back"}]
                if stack_depth > 2:
                    nav_row.append(
                        {"text": "🏠 Меню", "callback_data": "nav:home"}
                    )
                rows.append(nav_row)

        return {"inline_keyboard": rows}

    async def navigate(
        self,
        session: AsyncSession,
        user_id: int | str,
        chat_id: int | str,
        screen_id: str,
        payload: dict[str, Any] | None = None,
        push_to_history: bool = True,
        force_new_message: bool = False,
        anchor_message_id: int | None = None,
        send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
        send_api_fn: Callable[..., Awaitable[Any]] | None = None,
    ) -> Screen:
        if screen_id not in self._renderers:
            raise KeyError(f"Unknown screen_id: {screen_id}")

        default_title, renderer = self._renderers[screen_id]
        screen = await renderer(session, user_id, chat_id, payload or {})
        title = screen.title or default_title

        if push_to_history:
            await self.stack.push(
                user_id,
                screen_id,
                payload=payload,
                title=title,
                session=session,
            )

        crumbs = await self.stack.breadcrumbs(user_id, session=session)
        screen.breadcrumbs = crumbs
        depth = len(crumbs)

        full_text = screen.text
        if depth > 1:
            crumb_bar = " › ".join(tg_esc(c) for c in crumbs)
            full_text = f"🧭 <i>{crumb_bar}</i>\n\n{screen.text}"

        final_markup = self._with_navigation_buttons(screen.reply_markup, depth)
        rendered_screen = Screen(
            text=full_text,
            reply_markup=final_markup,
            parse_mode=screen.parse_mode,
            title=title,
            breadcrumbs=crumbs,
        )

        if anchor_message_id is not None:
            await self.set_anchor_id(user_id, anchor_message_id, session=session)

        target_msg_id = (
            None
            if force_new_message
            else (
                anchor_message_id
                or await self.get_anchor_id(user_id, session=session)
            )
        )

        sender = send_msg_fn or send_tg_message
        api_caller = send_api_fn or send_tg_api

        if target_msg_id is not None:
            edit_res = await api_caller(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": target_msg_id,
                    "text": rendered_screen.text,
                    "parse_mode": rendered_screen.parse_mode,
                    "disable_web_page_preview": True,
                    "reply_markup": rendered_screen.reply_markup,
                },
            )
            if isinstance(edit_res, dict) and edit_res.get("ok") is False:
                desc = str(edit_res.get("description") or "").lower()
                if "message is not modified" in desc:
                    return rendered_screen
                sent = await sender(
                    chat_id,
                    rendered_screen.text,
                    rendered_screen.reply_markup,
                )
                if isinstance(sent, dict) and isinstance(sent.get("result"), dict):
                    new_id = sent["result"].get("message_id")
                    if new_id:
                        await self.set_anchor_id(user_id, int(new_id), session=session)
            return rendered_screen

        sent = await sender(
            chat_id,
            rendered_screen.text,
            rendered_screen.reply_markup,
        )
        if isinstance(sent, dict) and isinstance(sent.get("result"), dict):
            new_id = sent["result"].get("message_id")
            if new_id:
                await self.set_anchor_id(user_id, int(new_id), session=session)
        return rendered_screen

    async def back(
        self,
        session: AsyncSession,
        user_id: int | str,
        chat_id: int | str,
        anchor_message_id: int | None = None,
        send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
        send_api_fn: Callable[..., Awaitable[Any]] | None = None,
    ) -> Screen | None:
        """Pop current screen from LIFO stack and render previous screen."""
        await self.stack.pop(user_id, session=session)
        prev = await self.stack.peek(user_id, session=session)
        if not prev:
            return await self.navigate(
                session,
                user_id=user_id,
                chat_id=chat_id,
                screen_id="home",
                payload={},
                push_to_history=True,
                anchor_message_id=anchor_message_id,
                send_msg_fn=send_msg_fn,
                send_api_fn=send_api_fn,
            )

        return await self.navigate(
            session,
            user_id=user_id,
            chat_id=chat_id,
            screen_id=prev["screen_id"],
            payload=prev.get("payload") or {},
            push_to_history=False,
            anchor_message_id=anchor_message_id,
            send_msg_fn=send_msg_fn,
            send_api_fn=send_api_fn,
        )


# ===================== Built-in Screen Renderers =====================

async def render_home_screen(
    session: AsyncSession,
    user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    text = (
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
    rows: list[list[dict[str, Any]]] = [
        [
            {"text": "💡 Банк идей", "callback_data": "nav:open:ideas"},
            {"text": "📅 Календарь", "callback_data": "nav:open:calendar"},
        ],
        [
            {"text": "👤 Профиль", "callback_data": "nav:open:profile"},
            {"text": "📋 Списки", "callback_data": "nav:open:list"},
        ],
    ]
    webapp_btn = tg_webapp_button("Открыть портал")
    if webapp_btn:
        rows.insert(0, [webapp_btn])
    return Screen(
        text=text,
        reply_markup={"inline_keyboard": rows},
        title="🏠 Главная",
    )


async def render_ideas_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    portal_rows = await get_portal_rows(session)
    text, base_kb = format_ideas_text(portal_rows)
    kb_rows: list[list[dict[str, Any]]] = []
    if base_kb and isinstance(base_kb.get("inline_keyboard"), list):
        kb_rows.extend(base_kb["inline_keyboard"])

    stages_present = [
        st
        for st in IDEA_STAGE_ORDER
        if any(r.get("stage") == st for r in portal_rows)
    ]
    stage_buttons = [
        {"text": f"📂 {st}", "callback_data": f"nav:stage:{st}"}
        for st in stages_present[:6]
    ]
    for i in range(0, len(stage_buttons), 2):
        kb_rows.append(stage_buttons[i : i + 2])

    return Screen(
        text=text,
        reply_markup={"inline_keyboard": kb_rows},
        title="💡 Банк идей",
    )


async def render_ideas_stage_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    payload: dict[str, Any],
) -> Screen:
    stage = str(payload.get("stage") or "Идея")
    portal_rows = [
        r for r in await get_portal_rows(session) if r.get("stage") == stage
    ]
    if not portal_rows:
        return Screen(
            text=f"<b>Этап «{tg_esc(stage)}»</b>\nКарточек на этом этапе пока нет.",
            reply_markup={"inline_keyboard": []},
            title=f"Этап: {stage}",
        )
    lines = [f"<b>Этап «{tg_esc(stage)}»</b> · {len(portal_rows)}", ""]
    kb_rows: list[list[dict[str, Any]]] = []
    for r in portal_rows[:10]:
        card_id = str(r.get("_id") or "")
        title = r.get("title") or "Без названия"
        club = f" — {tg_esc(r['club'])}" if r.get("club") else ""
        lines.append(f"• <b>{tg_esc(title)}</b>{club}")
        if card_id:
            kb_rows.append(
                [
                    {
                        "text": f"📄 {tg_cut(title, 32)}",
                        "callback_data": f"nav:card:{card_id[:48]}",
                    }
                ]
            )
    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title=f"📂 {stage}",
    )


async def render_idea_detail_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    payload: dict[str, Any],
) -> Screen:
    card_id = str(payload.get("card_id") or "")
    portal_rows = await get_portal_rows(session)
    card = next(
        (
            r
            for r in portal_rows
            if str(r.get("_id") or "") == card_id
            or str(r.get("_id") or "").startswith(card_id)
        ),
        None,
    )
    if not card:
        return Screen(
            text="Карточка не найдена.",
            reply_markup={"inline_keyboard": []},
            title="Карточка",
        )
    title = card.get("title") or card_id
    lines = [
        f"<b>{tg_esc(title)}</b>",
        f"Этап: <b>{tg_esc(card.get('stage') or 'Идея')}</b>",
    ]
    for field_key in ("club", "date", "contact", "owner", "what", "why", "who", "where", "how", "need", "goal"):
        val = card.get(field_key)
        if not val:
            continue
        disp = tg_date_fmt(val) if field_key == "date" else tg_cut(val, 300)
        label = TG_LABELS.get(field_key, field_key)
        lines.append(f"<b>{tg_esc(label)}:</b> {tg_esc(disp)}")

    kb_rows: list[list[dict[str, Any]]] = []
    btn = tg_webapp_button("Открыть карточку", f"sel={quote(str(card.get('_id') or card_id))}")
    if btn:
        kb_rows.append([btn])
    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title=tg_cut(title, 24),
    )


async def render_calendar_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    payload: dict[str, Any],
) -> Screen:
    today_iso = payload.get("today_iso")
    portal_rows = await get_portal_rows(session)
    text, base_kb = format_calendar_text(portal_rows, today_iso=today_iso)
    return Screen(
        text=text,
        reply_markup=base_kb or {"inline_keyboard": []},
        title="📅 Календарь",
    )


async def render_profile_screen(
    session: AsyncSession,
    user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    contact = (
        await session.execute(
            select(Contact).where(Contact.tg_id == str(user_id))
        )
    ).scalar_one_or_none()
    p = contact_to_profile(contact)
    miss = get_missing_profile_fields(p)
    if not contact:
        text = (
            "<b>Профиль участника</b>\n\n"
            "Вы ещё не зарегистрированы. Нажмите /register или откройте профиль в портале."
        )
    else:
        status_str = (
            f"⚠️ Не заполнено: {', '.join(miss)}"
            if miss
            else "✅ Профиль полностью заполнен"
        )
        text = (
            f"<b>Профиль участника</b>\n{status_str}\n\n"
            f"<b>Имя:</b> {tg_esc(full_name_from_profile(p) or '—')}\n"
            f"<b>Клуб:</b> {tg_esc(p.get('club') or '—')}\n"
            f"<b>Роль:</b> {tg_esc(p.get('role') or '—')}\n"
            f"<b>Телефон:</b> {tg_esc(p.get('phone') or '—')}\n"
            f"<b>Email:</b> {tg_esc(p.get('email') or '—')}"
        )
    kb_rows: list[list[dict[str, Any]]] = []
    btn = tg_webapp_button("Открыть профиль", "tab=me")
    if btn:
        kb_rows.append([btn])
    return Screen(
        text=text,
        reply_markup={"inline_keyboard": kb_rows},
        title="👤 Профиль",
    )


async def render_list_screen(
    session: AsyncSession,
    user_id: int | str,
    _chat_id: int | str,
    payload: dict[str, Any],
) -> Screen:
    evs = await get_managed_events(
        session, {"id": user_id}, today_iso=payload.get("today_iso")
    )
    if not evs:
        text = (
            "Списков пока нет. Команда работает для штаба и для организаторов клуба "
            "(роль «Организатор» в /register), когда на события клуба кто-то записался."
        )
        return Screen(
            text=text,
            reply_markup={"inline_keyboard": []},
            title="📋 Списки",
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
    return Screen(
        text="<b>Списки участников</b>\nНажмите на событие, пришлю таблицу CSV (открывается в Excel и Google Таблицах).",
        reply_markup={"inline_keyboard": inline_rows},
        title="📋 Списки",
    )


def create_default_navigator(
    state_store: StateStore = default_state_store,
    redis_client: Redis | None = None,
) -> AnchorNavigator:
    nav = AnchorNavigator(state_store=state_store, redis_client=redis_client)
    nav.register_screen("home", "🏠 Главная", render_home_screen)
    nav.register_screen("ideas", "💡 Банк идей", render_ideas_screen)
    nav.register_screen("ideas_stage", "📂 Этап", render_ideas_stage_screen)
    nav.register_screen("idea_detail", "📄 Карточка", render_idea_detail_screen)
    nav.register_screen("calendar", "📅 Календарь", render_calendar_screen)
    nav.register_screen("profile", "👤 Профиль", render_profile_screen)
    nav.register_screen("list", "📋 Списки", render_list_screen)
    return nav


default_navigator = create_default_navigator()
bot_router = Router(name="sunday_open_anchor_router")
