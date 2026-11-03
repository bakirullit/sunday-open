from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
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
    TG_STEPS,
    StateStore,
    contact_to_profile,
    default_state_store,
    format_calendar_text,
    format_ideas_text,
    full_name_from_profile,
    get_managed_events,
    get_missing_profile_fields,
    get_portal_rows,
    tg_days_from_today,
)
from backend.models import BotUser, Contact, Proposal, Record, Rsvp
from backend.moderation import (
    TG_LABELS,
    compute_proposal_diff,
    get_kv,
    is_tg_moderator,
    send_tg_api,
    send_tg_message,
    set_kv,
    tg_cut,
    tg_date_fmt,
    tg_esc,
    tg_table_label,
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

    async def render_screen(
        self,
        session: AsyncSession,
        user_id: int | str,
        chat_id: int | str,
        screen: Screen,
        anchor_message_id: int | None = None,
        force_new_message: bool = False,
        send_msg_fn: Callable[..., Awaitable[Any]] | None = None,
        send_api_fn: Callable[..., Awaitable[Any]] | None = None,
    ) -> Screen:
        """Render a declarative Screen directly into the single persistent anchor message."""
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
                    "text": screen.text,
                    "parse_mode": screen.parse_mode,
                    "disable_web_page_preview": True,
                    "reply_markup": screen.reply_markup,
                },
            )
            if isinstance(edit_res, dict) and edit_res.get("ok") is False:
                desc = str(edit_res.get("description") or "").lower()
                if "message is not modified" in desc:
                    return screen
                sent = await sender(
                    chat_id,
                    screen.text,
                    screen.reply_markup,
                )
                if isinstance(sent, dict) and isinstance(sent.get("result"), dict):
                    new_id = sent["result"].get("message_id")
                    if new_id:
                        await self.set_anchor_id(user_id, int(new_id), session=session)
            return screen

        sent = await sender(
            chat_id,
            screen.text,
            screen.reply_markup,
        )
        if isinstance(sent, dict) and isinstance(sent.get("result"), dict):
            new_id = sent["result"].get("message_id")
            if new_id:
                await self.set_anchor_id(user_id, int(new_id), session=session)
        return screen


# ===================== AITU-Style Onboarding & Wizard Screens =====================

def get_club_info_screen() -> Screen:
    """Screen: Welcome / About Sunday Open Hub for unregistered users."""
    text = (
        "🚀 <b>Добро пожаловать в Sunday Open · Проекты!</b>\n\n"
        "<b>Sunday Open Hub</b> — единая проектная экосистема студенческих клубов и инициатив. "
        "Здесь клубы подают идеи мероприятий, защищают паспорта проектов и ведут общий календарь событий!\n\n"
        "✨ <b>Что дает регистрация:</b>\n"
        "• Подача идей и бронирование дат через интерактивный Anchor Wizard\n"
        "• Доступ к банку идей и календарю всех мероприятий\n"
        "• Личный профиль организатора и выгрузка списков участников (CSV)\n\n"
        "Чтобы начать пользоваться платформой, пройдите быструю регистрацию 👇"
    )
    rows: list[list[dict[str, Any]]] = [
        [{"text": "📝 Пройти регистрацию", "callback_data": "auth:start_reg"}],
        [{"text": "💡 Банк идей", "callback_data": "nav:open:ideas"}],
        [{"text": "📅 Календарь событий", "callback_data": "nav:open:calendar"}],
        [{"text": "📊 Штаб · План 4 воскресений", "callback_data": "nav:open:workspace"}],
    ]
    webapp_btn = tg_webapp_button("🌐 Открыть Hub (Web App)")
    if webapp_btn:
        rows.append([webapp_btn])
    return Screen(
        text=text,
        reply_markup={"inline_keyboard": rows},
        title="🏠 Главная",
    )


def get_authorized_menu_screen(
    full_name: str,
    club: str = "",
    role: str = "Участник",
    is_mod: bool = False,
    reminders_on: bool = True,
) -> Screen:
    """Screen: Main Authorized Hub Menu with role-based layout."""
    if is_mod:
        status_label = f"Staff / Модератор ({role}) 🛡️"
    elif role == "Организатор":
        status_label = "Организатор клуба ⚔️"
    elif role == "Волонтёр":
        status_label = "Волонтёр 🤝"
    else:
        status_label = "Участник 🎓"

    club_line = f"🏛 Клуб: <b>{tg_esc(club)}</b>\n" if club else ""
    text = (
        "🚀 <b>Sunday Open · Проекты — Главное меню</b>\n\n"
        f"Добро пожаловать, <b>{tg_esc(full_name or 'Участник')}</b>!\n"
        f"{club_line}"
        f"🔰 Статус аккаунта: <b>{status_label}</b>\n\n"
        "Выберите раздел:"
    )

    rem_status = "🔔 Напоминания: Вкл" if reminders_on else "🔕 Напоминания: Выкл"
    rows: list[list[dict[str, Any]]] = [
        [{"text": "💡 Подать идею (Anchor Wizard)", "callback_data": "wiz:new_idea"}],
        [
            {"text": "📚 Банк идей", "callback_data": "nav:open:ideas"},
            {"text": "📅 Календарь", "callback_data": "nav:open:calendar"},
        ],
        [
            {"text": "🎟 Мои билеты (QR)", "callback_data": "nav:open:my_tickets"},
            {"text": "📊 План штаба", "callback_data": "nav:open:workspace"},
        ],
    ]

    if role == "Организатор" or is_mod:
        rows.append([{"text": "📋 Списки участников (CSV)", "callback_data": "nav:open:list"}])

    if is_mod:
        rows.append([{"text": "🛡 Очередь модерации", "callback_data": "nav:open:moderation"}])

    rows.append([
        {"text": "👤 Мой профиль", "callback_data": "nav:open:profile"},
        {"text": rem_status, "callback_data": "act:toggle_remind"},
    ])

    webapp_btn = tg_webapp_button("🌐 Открыть Sunday Open Web Hub")
    if webapp_btn:
        rows.append([webapp_btn])

    return Screen(
        text=text,
        reply_markup={"inline_keyboard": rows},
        title="🏠 Главная",
    )


def get_choose_role_screen() -> Screen:
    """State: CHOOSE_ROLE - Select club status: Организатор, Участник, Волонтёр."""
    text = (
        "👤 <b>Выберите ваш статус в клубе:</b>\n\n"
        "Пожалуйста, укажите вашу роль для настройки профиля:\n\n"
        "⚔️ <b>Организатор</b>\n"
        "— Для руководителей и координаторов студенческих клубов\n"
        "— Подача идей, паспорт проекта и выгрузка списков участников (CSV)\n\n"
        "🎓 <b>Участник</b>\n"
        "— Для студентов и гостей мероприятий\n"
        "— Запись на события (RSVP), чекин по QR-коду и дайджест событий\n\n"
        "🤝 <b>Волонтёр</b>\n"
        "— Для команды помощи в проведении мероприятий на площадке"
    )
    rows = [
        [{"text": "⚔️ Организатор", "callback_data": "auth:role:Организатор"}],
        [{"text": "🎓 Участник", "callback_data": "auth:role:Участник"}],
        [{"text": "🤝 Волонтёр", "callback_data": "auth:role:Волонтёр"}],
        [{"text": "◀️ Назад", "callback_data": "nav:home"}],
    ]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="👤 Роль")


def get_full_name_screen(role: str = "Участник", error_msg: str | None = None) -> Screen:
    """State: INPUT_FULL_NAME - Prompt for full name."""
    err_block = f"{error_msg}\n\n" if error_msg else ""
    text = (
        f"{err_block}"
        "📝 <b>Введите ФИО (Шаг 1 из 4)</b>\n\n"
        f"Роль: <b>{tg_esc(role)}</b> ✅\n\n"
        "Отправьте ваше <b>Имя и Фамилию</b> ответным сообщением в чат.\n\n"
        "⚠️ <i>Требования: минимум 2 слова. Ваше сообщение будет удалено сборщиком мусора, экран обновится на месте.</i>\n"
        "<i>Пример: Алихан Болатов</i>"
    )
    rows = [[{"text": "🚫 Отмена", "callback_data": "auth:cancel"}]]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="📝 ФИО")


def get_phone_screen(role: str, full_name: str, error_msg: str | None = None) -> Screen:
    """State: INPUT_PHONE - Prompt for phone number with contact button info."""
    err_block = f"{error_msg}\n\n" if error_msg else ""
    text = (
        f"{err_block}"
        "📱 <b>Отправьте номер телефона (Шаг 2 из 4)</b>\n\n"
        f"Роль: <b>{tg_esc(role)}</b> ✅\n"
        f"ФИО: <b>{tg_esc(full_name)}</b> ✅\n\n"
        "Нажмите кнопку <b>«📱 Поделиться контактом»</b> внизу экрана "
        "или напишите номер вручную в международном формате E.164 (например, <code>+77011234567</code>):"
    )
    rows = [[{"text": "🚫 Отмена", "callback_data": "auth:cancel"}]]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="📱 Телефон")


def get_phone_reply_keyboard() -> dict[str, Any]:
    """Reply markup with Share Contact button."""
    return {
        "keyboard": [
            [{"text": "📱 Поделиться контактом", "request_contact": True}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def get_email_screen(role: str, full_name: str, phone: str, error_msg: str | None = None) -> Screen:
    """State: INPUT_EMAIL - Prompt for email address."""
    err_block = f"{error_msg}\n\n" if error_msg else ""
    text = (
        f"{err_block}"
        "📧 <b>Введите адрес электронной почты (Шаг 3 из 4)</b>\n\n"
        f"Роль: <b>{tg_esc(role)}</b> ✅\n"
        f"ФИО: <b>{tg_esc(full_name)}</b> ✅\n"
        f"Телефон: <code>{tg_esc(phone)}</code> ✅\n\n"
        "Введите адрес вашей электронной почты для связи и уведомлений:\n\n"
        "<i>Пример: student@astanait.edu.kz или user@gmail.com</i>"
    )
    rows = [
        [
            {"text": "⏭️ Пропустить", "callback_data": "auth:skip_email"},
            {"text": "🚫 Отмена", "callback_data": "auth:cancel"},
        ]
    ]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="📧 Email")


def get_club_screen(
    role: str,
    full_name: str,
    phone: str,
    email: str = "",
    error_msg: str | None = None,
) -> Screen:
    """State: INPUT_CLUB - Prompt for club name."""
    err_block = f"{error_msg}\n\n" if error_msg else ""
    email_disp = email if email else "не указан"
    text = (
        f"{err_block}"
        "🏛 <b>Название вашего клуба (Шаг 4 из 4)</b>\n\n"
        f"Роль: <b>{tg_esc(role)}</b> ✅\n"
        f"ФИО: <b>{tg_esc(full_name)}</b> ✅\n"
        f"Телефон: <code>{tg_esc(phone)}</code> ✅\n"
        f"Email: <code>{tg_esc(email_disp)}</code> ✅\n\n"
        "Напишите название студенческого клуба или команды, которую вы представляете:\n\n"
        "<i>Пример: AITU Gaming, Music Club, Debate Club</i>"
    )
    rows = [[{"text": "🚫 Отмена", "callback_data": "auth:cancel"}]]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="🏛 Клуб")


def get_registration_cancelled_screen() -> Screen:
    """Screen: Registration Cancelled."""
    text = (
        "❌ <b>Регистрация отменена</b>\n\n"
        "Все введенные данные были сброшены. Вы можете начать процесс заново в любое удобное время."
    )
    rows = [
        [{"text": "📝 Пройти регистрацию", "callback_data": "auth:start_reg"}],
        [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
    ]
    return Screen(text=text, reply_markup={"inline_keyboard": rows}, title="❌ Отменено")


def get_wizard_step_screen(
    d: dict[str, Any],
    error_msg: str | None = None,
    occupied_dates: set[str] | None = None,
    today_iso: str | None = None,
) -> Screen:
    """Render 5W1H Idea Anchor Wizard step with progress summary & slot picker."""
    step_idx = int(d.get("step", 0))
    row_data = d.get("row") or {}
    field_key, prompt_text = TG_STEPS[step_idx]
    user_club = str(d.get("user_club") or "").strip()

    err_block = f"{error_msg}\n\n" if error_msg else ""
    lines = [
        f"{err_block}💡 <b>Подача идеи проекта (Шаг {step_idx + 1} из {len(TG_STEPS)})</b>",
        "",
    ]

    # Show already filled fields with checkmarks
    for idx in range(step_idx):
        k, _ = TG_STEPS[idx]
        val = row_data.get(k)
        label = TG_LABELS.get(k, k)
        if val:
            disp = tg_date_fmt(val) if k == "date" else tg_cut(str(val), 80)
            lines.append(f"• {tg_esc(label)}: <b>{tg_esc(disp)}</b> ✅")

    if step_idx > 0 and any(row_data.get(TG_STEPS[i][0]) for i in range(step_idx)):
        lines.append("")

    lines.append(f"👉 <b>{tg_esc(prompt_text)}</b>")
    lines.append("")
    lines.append("<i>💡 Отправьте ответ сообщением в чат (оно будет удалено сборщиком мусора, а экран обновится на месте).</i>")

    kb_rows: list[list[dict[str, Any]]] = []

    # Quick-pick user's registered club on Step 2 ('club')
    if field_key == "club" and user_club:
        kb_rows.append([
            {"text": f"🏛 Выбрать мой клуб: {tg_cut(user_club, 26)}", "callback_data": "wiz:use_club"}
        ])

    # Interactive 14-day slot picker on Step 3 ('date'), just like aitu-gaming-hub
    if field_key == "date":
        lines.append("\n📅 <b>Или выберите свободную дату нажатием кнопки:</b>\n🟢 — Свободно | 🔴 — Есть события")
        try:
            base_day = (
                date.fromisoformat(today_iso) if today_iso else date.today()
            ) + timedelta(days=28)
        except Exception:
            base_day = date.today() + timedelta(days=28)

        occ = occupied_dates or set()
        slot_row: list[dict[str, Any]] = []
        for i in range(10):
            slot_d = base_day + timedelta(days=i)
            iso_s = slot_d.isoformat()
            d_label = slot_d.strftime("%d.%m")
            badge = "🔴" if iso_s in occ else "🟢"
            slot_row.append({"text": f"{d_label} {badge}", "callback_data": f"wiz:date:{iso_s}"})
            if len(slot_row) == 2:
                kb_rows.append(slot_row)
                slot_row = []
        if slot_row:
            kb_rows.append(slot_row)

    # Navigation controls
    nav_row: list[dict[str, Any]] = []
    if step_idx > 0:
        nav_row.append({"text": "◀️ Назад", "callback_data": "wiz:prev"})
        nav_row.append({"text": "⏭️ Пропустить шаг", "callback_data": "wiz:skip"})
    if nav_row:
        kb_rows.append(nav_row)

    kb_rows.append([{"text": "🚫 Отмена", "callback_data": "wiz:cancel"}])

    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title=f"💡 Шаг {step_idx + 1}/{len(TG_STEPS)}",
    )


def get_wizard_confirm_screen(d: dict[str, Any]) -> Screen:
    """Step 10: Final review card before submitting idea to headquarters."""
    row_data = d.get("row") or {}
    lines = ["📋 <b>Проверьте заявку перед отправкой</b>", ""]
    for s_key, _ in TG_STEPS:
        val = row_data.get(s_key)
        if not val:
            continue
        display_val = tg_date_fmt(val) if s_key == "date" else val
        lines.append(
            f"• <b>{tg_esc(TG_LABELS.get(s_key, s_key))}:</b> {tg_esc(display_val)}"
        )
    lines.append("")
    lines.append("Отправить карточку проекта на согласование в штаб Sunday Open?")

    kb_rows = [
        [{"text": "🚀 Отправить в штаб", "callback_data": "n:send"}],
        [
            {"text": "◀️ Назад", "callback_data": "wiz:prev"},
            {"text": "🔄 С начала", "callback_data": "wiz:new_idea"},
        ],
        [{"text": "❌ Отмена", "callback_data": "n:cancel"}],
    ]
    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title="📋 Проверка заявки",
    )


def get_wizard_submitted_screen(card_id: str, title: str, is_pending: bool) -> Screen:
    """Screen rendered into creator's anchor after submitting idea."""
    status_badge = (
        "🟡 На рассмотрении в штабе (уведомлю в этом чате после проверки)"
        if is_pending
        else "🟢 Опубликовано в банке идей"
    )
    text = (
        "✅ <b>Заявка успешно отправлена!</b>\n\n"
        f"• <b>Проект:</b> <b>{tg_esc(title)}</b>\n"
        f"• <b>Статус:</b> {status_badge}\n\n"
        "Карточку можно дополнить в портале: паспорт, контакты, детали."
    )
    kb_rows: list[list[dict[str, Any]]] = []
    btn = tg_webapp_button("🌐 Открыть карточку в Web Hub", f"sel={quote(card_id)}")
    if btn:
        kb_rows.append([btn])
    kb_rows.append([{"text": "💡 Банк идей", "callback_data": "nav:open:ideas"}])
    kb_rows.append([{"text": "🏠 Главное меню", "callback_data": "nav:home"}])
    return Screen(
        text=text,
        reply_markup={"inline_keyboard": kb_rows},
        title="✅ Отправлено",
    )


# ===================== Built-in Screen Renderers =====================

async def render_home_screen(
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
    bot_user = (
        await session.execute(
            select(BotUser).where(BotUser.tg_id == str(user_id))
        )
    ).scalar_one_or_none()
    reminders_on = bool(bot_user.reminders) if bot_user else True
    is_mod = await is_tg_moderator(session, user_id)

    if contact and (contact.first_name or contact.club):
        p = contact_to_profile(contact)
        return get_authorized_menu_screen(
            full_name=full_name_from_profile(p),
            club=p.get("club") or "",
            role=p.get("role") or "Участник",
            is_mod=is_mod,
            reminders_on=reminders_on,
        )

    # Even if unregistered, if user is moderator show authorized menu
    if is_mod:
        return get_authorized_menu_screen(
            full_name=bot_user.name if bot_user else "Модератор",
            club="Штаб Sunday Open",
            role="Организатор",
            is_mod=True,
            reminders_on=reminders_on,
        )

    return get_club_info_screen()


async def render_ideas_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    portal_rows = await get_portal_rows(session)
    text, base_kb = format_ideas_text(portal_rows)
    kb_rows: list[list[dict[str, Any]]] = [
        [{"text": "💡 Подать идею (Anchor Wizard)", "callback_data": "wiz:new_idea"}]
    ]
    if base_kb and isinstance(base_kb.get("inline_keyboard"), list):
        for r in base_kb["inline_keyboard"]:
            kb_rows.append(r)

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
    user_id: int | str,
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
    full_card_id = str(card.get("_id") or card_id)
    title = card.get("title") or full_card_id
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

    # Check RSVP status and participant count in PostgreSQL
    rsvps_res = await session.execute(
        select(Rsvp).where(Rsvp.event_id == full_card_id, Rsvp.status == "going")
    )
    going_rsvps = list(rsvps_res.scalars().all())
    user_rsvp = next((r for r in going_rsvps if str(r.tg_id) == str(user_id)), None)

    kb_rows: list[list[dict[str, Any]]] = []
    if card.get("date") and card.get("stage") not in ("Отклонена", "Закрыт"):
        lines.append("")
        lines.append(f"👥 <b>Записались:</b> {len(going_rsvps)} чел.")
        if user_rsvp:
            checked_str = " (отмечен на входе ✅)" if user_rsvp.checked_in_at else ""
            lines.append(
                f"🎟 <b>Ваш статус:</b> Вы записаны · билет <code>{tg_esc(user_rsvp.code)}</code>{checked_str}"
            )
            kb_rows.append([
                {"text": "📲 Прислать QR-билет в чат", "callback_data": f"rsvp:qr:{full_card_id[:48]}"}
            ])
            kb_rows.append([
                {"text": "❌ Отменить запись", "callback_data": f"rsvp:cancel:{full_card_id[:48]}"}
            ])
        else:
            kb_rows.append([
                {"text": "🎟 Записаться на событие (RSVP)", "callback_data": f"rsvp:go:{full_card_id[:48]}"}
            ])

    btn = tg_webapp_button("Открыть карточку", f"sel={quote(full_card_id)}")
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
    kb_rows: list[list[dict[str, Any]]] = []

    upcoming = [
        r
        for r in portal_rows
        if r.get("date")
        and r.get("stage") not in ("Отклонена", "Закрыт")
        and tg_days_from_today(str(r["date"]), today_iso=today_iso) >= 0
    ]
    upcoming.sort(key=lambda r: str(r.get("date") or ""))
    for r in upcoming[:8]:
        card_id = str(r.get("_id") or "")
        title = r.get("title") or "Событие"
        d_fmt = tg_date_fmt(r.get("date"))[:5]
        if card_id:
            kb_rows.append([
                {"text": f"📄 {d_fmt} · {tg_cut(title, 26)}", "callback_data": f"nav:card:{card_id[:48]}"}
            ])

    if base_kb and isinstance(base_kb.get("inline_keyboard"), list):
        for r in base_kb["inline_keyboard"]:
            kb_rows.append(r)

    return Screen(
        text=text,
        reply_markup={"inline_keyboard": kb_rows},
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
    bot_user = (
        await session.execute(
            select(BotUser).where(BotUser.tg_id == str(user_id))
        )
    ).scalar_one_or_none()
    reminders_on = bool(bot_user.reminders) if bot_user else True
    rem_status = "🔔 Напоминания: Вкл" if reminders_on else "🔕 Напоминания: Выкл"

    p = contact_to_profile(contact)
    miss = get_missing_profile_fields(p)
    if not contact or not (contact.first_name or contact.club):
        text = (
            "👤 <b>Профиль участника Sunday Open</b>\n\n"
            "Вы ещё не зарегистрированы в клубе.\n"
            "Нажмите <b>«📝 Пройти регистрацию»</b> ниже для быстрой настройки профиля."
        )
        kb_rows = [
            [{"text": "📝 Пройти регистрацию", "callback_data": "auth:start_reg"}],
            [{"text": rem_status, "callback_data": "act:toggle_remind"}],
        ]
    else:
        status_str = (
            f"⚠️ Не заполнено: {', '.join(miss)}"
            if miss
            else "✅ Профиль полностью заполнен"
        )
        text = (
            f"👤 <b>Профиль участника Sunday Open</b>\n{status_str}\n\n"
            f"• ФИО: <b>{tg_esc(full_name_from_profile(p) or '—')}</b>\n"
            f"• Клуб: <b>{tg_esc(p.get('club') or '—')}</b>\n"
            f"• Роль: <b>{tg_esc(p.get('role') or '—')}</b>\n"
            f"• Телефон: <code>{tg_esc(p.get('phone') or '—')}</code>\n"
            f"• Email: <code>{tg_esc(p.get('email') or '—')}</code>\n"
            f"• Уведомления: <b>{'Включены 🔔' if reminders_on else 'Выключены 🔕'}</b>"
        )
        kb_rows = [
            [{"text": "📝 Изменить данные профиля", "callback_data": "auth:start_reg"}],
            [{"text": rem_status, "callback_data": "act:toggle_remind"}],
        ]
    btn = tg_webapp_button("🌐 Открыть профиль в Web Hub", "tab=me")
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
            "<b>Списки участников</b>\n\n"
            "Списков пока нет. Раздел доступен организаторам клубов "
            "и штабу, когда на события вашего клуба регистрируются участники."
        )
        return Screen(
            text=text,
            reply_markup={"inline_keyboard": []},
            title="📋 Списки",
        )
    inline_rows = [
        [
            {
                "text": f"📥 {tg_date_fmt(e['date'])[:5]} · {tg_cut(e['title'], 30)} · {e['going']}"
                + (f" (пришли {e['inside']})" if e["inside"] else ""),
                "callback_data": f"l:{e['id'][:60]}",
            }
        ]
        for e in evs[:20]
    ]
    return Screen(
        text="<b>Списки участников</b>\nНажмите на событие, чтобы скачать таблицу CSV (Excel):",
        reply_markup={"inline_keyboard": inline_rows},
        title="📋 Списки",
    )


async def render_my_tickets_screen(
    session: AsyncSession,
    user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    rsvps_res = await session.execute(
        select(Rsvp)
        .where(Rsvp.tg_id == str(user_id), Rsvp.status == "going")
        .order_by(Rsvp.date.asc())
    )
    mine = list(rsvps_res.scalars().all())
    if not mine:
        text = (
            "🎟 <b>Мои билеты (QR)</b>\n\n"
            "У вас пока нет активных записей на события.\n"
            "Откройте <b>«📅 Календарь событий»</b> или <b>«📚 Банк идей»</b>, "
            "выберите мероприятие и нажмите <b>«🎟 Записаться на событие (RSVP)»</b>."
        )
        kb_rows = [
            [{"text": "📅 Открыть календарь событий", "callback_data": "nav:open:calendar"}],
            [{"text": "📚 Открыть банк идей", "callback_data": "nav:open:ideas"}],
        ]
        return Screen(
            text=text,
            reply_markup={"inline_keyboard": kb_rows},
            title="🎟 Мои билеты",
        )

    lines = [f"🎟 <b>Мои билеты (QR)</b> · активных: {len(mine)}", ""]
    kb_rows = []
    for r in mine[:10]:
        d_fmt = tg_date_fmt(r.date)[:5] if r.date else ""
        chk = " · ✅ отмечен на входе" if r.checked_in_at else ""
        lines.append(
            f"• <b>{tg_esc(r.title)}</b> ({d_fmt}) — код <code>{tg_esc(r.code)}</code>{chk}"
        )
        kb_rows.append([
            {"text": f"📲 QR · {tg_cut(r.title, 20)}", "callback_data": f"rsvp:qr:{r.event_id[:48]}"},
            {"text": "📄 Карточка", "callback_data": f"nav:card:{r.event_id[:48]}"},
        ])

    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title="🎟 Мои билеты",
    )


async def render_workspace_screen(
    session: AsyncSession,
    _user_id: int | str,
    _chat_id: int | str,
    payload: dict[str, Any],
) -> Screen:
    tab = str(payload.get("tab") or "sundays")
    res = await session.execute(
        select(Record).where(Record.tbl == tab, Record.deleted.is_(False))
    )
    recs = [r.row for r in res.scalars().all() if r.row]
    recs.sort(key=lambda x: int(x.get("_o") or 0))

    lines: list[str] = []
    if tab == "sundays":
        lines.append("📊 <b>Штаб Sunday Open · План 4 воскресений</b>\n")
        for r in recs:
            num = r.get("num") or ""
            date_s = r.get("date") or ""
            theme = r.get("theme") or ""
            mission = r.get("mission") or ""
            target = r.get("target") or ""
            evs = r.get("events") or []
            lines.append(f"🗓 <b>{tg_esc(num)} ({tg_esc(date_s)}) — {tg_esc(theme)}</b>")
            if mission:
                lines.append(f"<i>{tg_esc(mission)}</i>")
            for ev in evs:
                if isinstance(ev, dict):
                    lines.append(
                        f"  • <b>{tg_esc(ev.get('name'))}</b> — {tg_esc(ev.get('sub'))} ({tg_esc(ev.get('loc'))})"
                    )
            if target:
                lines.append(f"  🎯 Ориентир: {tg_esc(target)}")
            lines.append("")
    elif tab == "steps":
        lines.append("🚀 <b>Штаб Sunday Open · Первые шаги</b>\n")
        for r in recs[:12]:
            st_badge = "✅" if r.get("status") == "Готово" else ("⏳" if r.get("status") == "В работе" else "⬜")
            lines.append(
                f"{st_badge} <b>{tg_esc(r.get('step'))}</b> (до {tg_esc(r.get('deadline'))})\n"
                f"   👤 {tg_esc(r.get('who'))} · {tg_esc(r.get('what'))}"
            )
    elif tab == "bookings":
        lines.append("🏛 <b>Штаб Sunday Open · Брони локаций</b>\n")
        for r in recs[:15]:
            lines.append(
                f"• <b>{tg_esc(r.get('date'))}</b> · {tg_esc(r.get('event'))}\n"
                f"  📍 {tg_esc(r.get('room'))} ({tg_esc(r.get('time'))}) · {tg_esc(r.get('status'))}"
            )
    elif tab == "briefs":
        lines.append("📋 <b>Штаб Sunday Open · Брифы 5W1H</b>\n")
        for r in recs[:8]:
            lines.append(
                f"• <b>{tg_esc(r.get('name'))}</b> ({tg_esc(r.get('when'))})\n"
                f"  📍 {tg_esc(r.get('where'))}\n"
                f"  💡 {tg_cut(r.get('what') or '', 120)}"
            )

    if not recs:
        lines.append("Записей в этом разделе пока нет.")

    kb_rows = [
        [
            {"text": ("🔘 " if tab == "sundays" else "") + "🗓 4 воскресенья", "callback_data": "ws:tab:sundays"},
            {"text": ("🔘 " if tab == "steps" else "") + "🚀 Шаги запуска", "callback_data": "ws:tab:steps"},
        ],
        [
            {"text": ("🔘 " if tab == "bookings" else "") + "🏛 Локации", "callback_data": "ws:tab:bookings"},
            {"text": ("🔘 " if tab == "briefs" else "") + "📋 Брифы 5W1H", "callback_data": "ws:tab:briefs"},
        ],
    ]
    return Screen(
        text="\n".join(lines).strip(),
        reply_markup={"inline_keyboard": kb_rows},
        title="📊 План штаба",
    )


async def render_moderation_screen(
    session: AsyncSession,
    user_id: int | str,
    _chat_id: int | str,
    _payload: dict[str, Any],
) -> Screen:
    if not await is_tg_moderator(session, user_id):
        return Screen(
            text="Раздел доступен только модераторам.",
            reply_markup={"inline_keyboard": []},
            title="🛡 Модерация",
        )
    proposals = (
        await session.execute(
            select(Proposal)
            .where(Proposal.status == "pending")
            .order_by(Proposal.created_at.asc())
            .limit(10)
        )
    ).scalars().all()

    if not proposals:
        return Screen(
            text="<b>🛡 Очередь модерации</b>\n\nВсе заявки рассмотрены! Новых правок нет. 🎉",
            reply_markup={"inline_keyboard": []},
            title="🛡 Модерация",
        )

    lines = [f"<b>🛡 Очередь модерации</b> · {len(proposals)} на рассмотрении:\n"]
    kb_rows: list[list[dict[str, Any]]] = []
    for p in proposals:
        row_obj = p.after if isinstance(p.after, dict) else (p.before if isinstance(p.before, dict) else {})
        title = (
            row_obj.get("title")
            or row_obj.get("name")
            or row_obj.get("event")
            or row_obj.get("step")
            or p.id
        )
        action_kind = (
            "удаление"
            if p.is_delete
            else ("новая карточка" if not p.before else "изменение")
        )
        tbl_label = tg_table_label(p.tbl)
        diffs = compute_proposal_diff(p)
        diff_preview = "; ".join(f"{k}: {tg_cut(v, 40)}" for k, v in list(diffs.items())[:3])
        lines.append(
            f"• <b>{tg_esc(title)}</b> ({tg_esc(tbl_label)} · {action_kind})"
            + (f"\n  <i>{tg_esc(diff_preview)}</i>" if diff_preview else "")
        )
        cb_suffix = f"{p.mutation_id}:{p.hash[:10]}"
        kb_rows.append([
            {"text": f"✅ {tg_cut(title, 20)}", "callback_data": f"a:{cb_suffix}"},
            {"text": "❌ Отклонить", "callback_data": f"r:{cb_suffix}"},
        ])

    return Screen(
        text="\n".join(lines),
        reply_markup={"inline_keyboard": kb_rows},
        title="🛡 Модерация",
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
    nav.register_screen("my_tickets", "🎟 Мои билеты", render_my_tickets_screen)
    nav.register_screen("workspace", "📊 План штаба", render_workspace_screen)
    nav.register_screen("profile", "👤 Профиль", render_profile_screen)
    nav.register_screen("list", "📋 Списки", render_list_screen)
    nav.register_screen("moderation", "🛡 Модерация", render_moderation_screen)
    return nav


default_navigator = create_default_navigator()
bot_router = Router(name="sunday_open_anchor_router")

