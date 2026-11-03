import asyncio
import random
import secrets
from typing import Any, Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import async_session_factory
from backend.moderation import get_kv, send_tg_api, set_kv


async def setup_sunday_bot(
    session: AsyncSession,
    api_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
) -> str:
    """
    One-time setup matching §8.3 and setupSundayBot in Code.gs:
    1. Generate TG_SECRET and 6-digit TG_MOD_CODE if missing.
    2. Call getMe and store TG_BOT_USERNAME.
    3. Call setWebhook with allowed_updates=[message, callback_query], drop_pending_updates, and secret_token.
    4. Call setMyCommands (register, new, ideas, calendar, list, remind, help).
    5. Call setChatMenuButton with Web App «Портал» = TG_WEBAPP_URL.
    6. Call setMyDescription.
    7. Print the moderator code.
    """
    caller = api_fn or send_tg_api
    if not settings.TG_BOT_TOKEN:
        raise RuntimeError("Добавьте TG_BOT_TOKEN в переменные окружения")
    if not settings.TG_WEBAPP_URL or not settings.TG_WEBAPP_URL.startswith("https://"):
        raise RuntimeError(
            "Добавьте TG_WEBAPP_URL: https-адрес портала на GitHub Pages"
        )
    if not settings.PUBLIC_BASE_URL:
        raise RuntimeError("Добавьте PUBLIC_BASE_URL: публичный адрес веб-сервиса")

    tg_secret = await get_kv(session, "TG_SECRET", "")
    if not tg_secret:
        tg_secret = secrets.token_hex(16)
        await set_kv(session, "TG_SECRET", tg_secret)

    tg_mod_code = await get_kv(session, "TG_MOD_CODE", "")
    if not tg_mod_code:
        tg_mod_code = str(random.randint(100000, 999999))
        await set_kv(session, "TG_MOD_CODE", tg_mod_code)

    me = await caller("getMe", {})
    if me and me.get("ok") and isinstance(me.get("result"), dict):
        username = me["result"].get("username") or ""
        if username:
            await set_kv(session, "TG_BOT_USERNAME", username)

    webhook_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/?tg={tg_secret}"
    hook = await caller(
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": tg_secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        },
    )
    if not hook or not hook.get("ok"):
        desc = hook.get("description") if isinstance(hook, dict) else "unknown error"
        raise RuntimeError(f"Telegram не принял webhook: {desc}")

    await caller(
        "setMyCommands",
        {
            "commands": [
                {"command": "register", "description": "Регистрация и контакты"},
                {"command": "new", "description": "Подать идею"},
                {"command": "ideas", "description": "Банк идей"},
                {"command": "calendar", "description": "Ближайшие события"},
                {"command": "list", "description": "Списки участников"},
                {"command": "remind", "description": "Напоминания вкл/выкл"},
                {"command": "help", "description": "Что умеет бот"},
            ]
        },
    )
    await caller(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": "Портал",
                "web_app": {"url": settings.TG_WEBAPP_URL},
            }
        },
    )
    await caller(
        "setMyDescription",
        {
            "description": "Sunday Open: банк идей, паспорт проекта и календарь клубов AITU. Правки проверяет штаб."
        },
    )

    msg = f"Бот готов. Модераторам: отправьте боту  /mod {tg_mod_code}"
    print(msg)
    return tg_mod_code


async def _main_async() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            await setup_sunday_bot(session)


def setup_bot_cli() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    setup_bot_cli()

