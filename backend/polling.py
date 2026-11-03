import asyncio
import logging
import random
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.bot import (
    StateStore,
    default_state_store,
    handle_tg_callback,
    handle_tg_message,
)
from backend.config import settings
from backend.database import async_session_factory
from backend.moderation import get_kv, send_tg_api, set_kv

logger = logging.getLogger("backend.polling")


async def dispatch_tg_update(
    session: AsyncSession,
    update: dict[str, Any],
    state_store: StateStore = default_state_store,
) -> bool:
    """Process an incoming update from polling matching webhook dispatch."""
    if not isinstance(update, dict):
        return False

    update_id = update.get("update_id")
    if update_id is not None:
        dedup_key = f"tgu{update_id}"
        if await state_store.get(dedup_key):
            return False
        await state_store.put(dedup_key, "1", ttl=21600)

    if "callback_query" in update and isinstance(update["callback_query"], dict):
        await handle_tg_callback(session, update["callback_query"], state_store=state_store)
        return True

    msg = update.get("message")
    if (
        isinstance(msg, dict)
        and msg.get("from")
        and isinstance(msg.get("chat"), dict)
        and msg["chat"].get("type") == "private"
        and (isinstance(msg.get("text"), str) or msg.get("contact"))
    ):
        await handle_tg_message(session, msg, state_store=state_store)
        return True

    return False


async def init_bot_profile(token: str) -> None:
    """Initialize bot commands, menu button, and kv state matching setupSundayBot."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            me_res = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if me_res.status_code == 200:
                me_data = me_res.json().get("result", {})
                username = me_data.get("username", "")
                first_name = me_data.get("first_name", "")
                logger.info("Bot authenticated: @%s (%s)", username, first_name)
                async with async_session_factory() as s:
                    async with s.begin():
                        if username:
                            await set_kv(s, "TG_BOT_USERNAME", username)
                        code = await get_kv(s, "TG_MOD_CODE", "")
                        if not code:
                            code = str(random.randint(100000, 999999))
                            await set_kv(s, "TG_MOD_CODE", code)
                        logger.info("Telegram moderator code: /mod %s", code)

            # Set commands
            await client.post(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                json={
                    "commands": [
                        {"command": "menu", "description": "Интерактивное меню (Anchor UI)"},
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

            # Set chat menu button if webapp url configured
            if settings.TG_WEBAPP_URL:
                await client.post(
                    f"https://api.telegram.org/bot{token}/setChatMenuButton",
                    json={
                        "menu_button": {
                            "type": "web_app",
                            "text": "Портал",
                            "web_app": {"url": settings.TG_WEBAPP_URL},
                        }
                    },
                )
    except Exception as exc:
        logger.warning("Bot profile initialization warning: %s", exc)


async def run_bot_polling(bot_token: str | None = None) -> None:
    """Continuously poll Telegram for updates in background."""
    token = bot_token or settings.TG_BOT_TOKEN
    if not token or token.startswith("123456:"):
        logger.info("No valid Telegram bot token configured; polling skipped.")
        return

    logger.info("Starting Telegram bot long-polling...")
    await init_bot_profile(token)

    offset = 0
    # Clear any old conflicting webhook so getUpdates succeeds
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                json={"drop_pending_updates": False},
            )
    except Exception as exc:
        logger.warning("Could not delete webhook before polling: %s", exc)

    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                payload: dict[str, Any] = {
                    "offset": offset,
                    "timeout": 20,
                    "allowed_updates": ["message", "callback_query"],
                }
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning("getUpdates returned HTTP %d: %s", resp.status_code, resp.text)
                    await asyncio.sleep(3)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    logger.warning("getUpdates error: %s", data.get("description"))
                    await asyncio.sleep(3)
                    continue

                updates = data.get("result", [])
                for upd in updates:
                    upd_id = upd.get("update_id", 0)
                    if upd_id >= offset:
                        offset = upd_id + 1

                    async with async_session_factory() as session:
                        async with session.begin():
                            try:
                                await dispatch_tg_update(session, upd)
                            except Exception as e:
                                logger.exception("Failed processing update %d: %s", upd_id, e)

            except asyncio.CancelledError:
                logger.info("Telegram polling cancelled.")
                break
            except Exception as exc:
                logger.error("Error in polling loop: %s", exc)
                await asyncio.sleep(2)

