import json
from typing import Any

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from backend.app import app
from backend.config import settings
from backend.models import Author, EmailNotified, Proposal, Record
from backend.moderation import (
    create_admin_token,
    send_sunday_notifications,
    set_kv,
    verify_admin_token,
)


@pytest.mark.asyncio
async def test_telegram_moderator_callbacks_and_author_notifications(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Test Telegram moderator buttons (✅ Одобрить / ❌ Отклонить),
    unauthorized moderator rejection, hash prefix verification, and author notifications.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")
    monkeypatch.setattr(settings, "TG_BOT_TOKEN", "123456:TEST_TOKEN")

    sent_messages: list[tuple[Any, str]] = []
    api_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_send_msg(chat_id: Any, html: str, reply_markup: Any = None, **kw: Any) -> dict[str, Any]:
        sent_messages.append((chat_id, html))
        return {"ok": True, "result": {"message_id": 999}}

    async def fake_send_api(method: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        api_calls.append((method, payload))
        return {"ok": True}

    from backend import bot as bot_mod
    from backend import moderation as mod_mod

    monkeypatch.setattr(bot_mod, "send_tg_message", fake_send_msg)
    monkeypatch.setattr(bot_mod, "send_tg_api", fake_send_api)
    monkeypatch.setattr(mod_mod, "send_tg_message", fake_send_msg)
    monkeypatch.setattr(mod_mod, "send_tg_api", fake_send_api)

    async with session_maker() as s:
        async with s.begin():
            await set_kv(s, "TG_SECRET", "wh_secret_123")
            await set_kv(
                s,
                "TG_MODERATORS",
                json.dumps([{"id": 777, "name": "Chief Mod"}]),
            )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        prop1 = {
            "protocol": 4,
            "table": "portal",
            "id": "portal-mod-1",
            "mutationId": "tg:portal-mod-1",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-mod-1", "_o": 1, "title": "Квиз Клуба", "stage": "Идея"},
        }
        prop2 = {
            "protocol": 4,
            "table": "portal",
            "id": "portal-mod-2",
            "mutationId": "tg:portal-mod-2",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-mod-2", "_o": 2, "title": "Турнир", "stage": "Идея"},
        }
        await client.post("/", content=json.dumps(prop1))
        await client.post("/", content=json.dumps(prop2))

        async with session_maker() as s:
            async with s.begin():
                s.add(
                    Author(
                        mutation_id="tg:portal-mod-1",
                        tg_id="555",
                        name="Author One",
                        tbl="portal",
                        row_id="portal-mod-1",
                    )
                )
                s.add(
                    Author(
                        mutation_id="tg:portal-mod-2",
                        tg_id="555",
                        name="Author One",
                        tbl="portal",
                        row_id="portal-mod-2",
                    )
                )
                p1 = (
                    await s.execute(
                        select(Proposal).where(Proposal.mutation_id == "tg:portal-mod-1")
                    )
                ).scalar_one()
                p2 = (
                    await s.execute(
                        select(Proposal).where(Proposal.mutation_id == "tg:portal-mod-2")
                    )
                ).scalar_one()
                h1 = p1.hash[:10]
                h2 = p2.hash[:10]
                ref1 = p1.mutation_id[:48]
                ref2 = p2.mutation_id[:48]

        # 1. Non-moderator tries to approve -> rejected with alert
        unauth_update = {
            "update_id": 1001,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 999, "first_name": "Intruder"},
                "message": {"message_id": 10, "chat": {"id": 999}, "text": "Prop"},
                "data": f"a:{ref1}:{h1}",
            },
        }
        await client.post("/?tg=wh_secret_123", content=json.dumps(unauth_update))
        assert any(
            m == "answerCallbackQuery" and "только модераторы" in p.get("text", "")
            for m, p in api_calls
        )

        # 2. Authorized moderator approves p1
        sent_messages.clear()
        auth_approve = {
            "update_id": 1002,
            "callback_query": {
                "id": "cb-2",
                "from": {"id": 777, "first_name": "Chief"},
                "message": {"message_id": 11, "chat": {"id": 777}, "text": "Prop 1"},
                "data": f"a:{ref1}:{h1}",
            },
        }
        await client.post("/?tg=wh_secret_123", content=json.dumps(auth_approve))
        assert any(
            str(chat_id) == "555" and "Штаб одобрил" in text
            for chat_id, text in sent_messages
        )

        # 3. Authorized moderator rejects p2
        sent_messages.clear()
        auth_reject = {
            "update_id": 1003,
            "callback_query": {
                "id": "cb-3",
                "from": {"id": 777, "first_name": "Chief"},
                "message": {"message_id": 12, "chat": {"id": 777}, "text": "Prop 2"},
                "data": f"r:{ref2}:{h2}",
            },
        }
        await client.post("/?tg=wh_secret_123", content=json.dumps(auth_reject))
        assert any(
            str(chat_id) == "555" and "Штаб отклонил" in text
            for chat_id, text in sent_messages
        )


@pytest.mark.asyncio
async def test_admin_web_panel_and_token_verification(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test /admin web panel diff rendering and proposal approval via signed session cookie."""
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")
    monkeypatch.setattr(settings, "SUNDAY_MODERATOR_EMAILS", '["admin@sunday.kz"]')

    token = create_admin_token("admin@sunday.kz")
    assert verify_admin_token(token) == "admin@sunday.kz"
    assert verify_admin_token(token + "tampered") is None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        prop = {
            "protocol": 4,
            "table": "portal",
            "id": "portal-web-admin",
            "mutationId": "web:admin:1",
            "baseVersion": 0,
            "del": False,
            "row": {"_id": "portal-web-admin", "_o": 1, "title": "Web Admin Card", "stage": "Идея"},
        }
        await client.post("/", content=json.dumps(prop))

        # Unauthenticated /admin returns 403
        r_unauth = await client.get("/admin")
        assert r_unauth.status_code == 403

        # Authenticated /admin shows pending proposal diff
        client.cookies.set("sunday_admin_token", token)
        r_auth = await client.get("/admin")
        assert r_auth.status_code == 200
        assert "Web Admin Card" in r_auth.text

        r_rev = await client.post(
            "/admin/review",
            data={
                "mutation_id": "web:admin:1",
                "action": "approve",
                "token": token,
            },
            follow_redirects=False,
        )
        assert r_rev.status_code in (200, 303)

        async with session_maker() as s:
            rec = (
                await s.execute(
                    select(Record).where(Record.id == "portal-web-admin")
                )
            ).scalar_one_or_none()
            assert rec is not None
            assert rec.version == 1


@pytest.mark.asyncio
async def test_5min_email_digest_deduplication_and_retry(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Verify §7.4 5-minute email digest:
    - Batches un-notified pending proposals into a single email.
    - Does not mark rows as notified if SMTP fails (so next run retries).
    - Marks rows in email_notified only after SMTP succeeds.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")
    monkeypatch.setattr(settings, "SUNDAY_MODERATOR_EMAILS", '["hq@sunday.kz"]')
    monkeypatch.setattr(settings, "SUNDAY_NOTIFICATION_EMAIL", "hq@sunday.kz")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for idx in (1, 2):
            await client.post(
                "/",
                content=json.dumps(
                    {
                        "protocol": 4,
                        "table": "portal",
                        "id": f"portal-dig-{idx}",
                        "mutationId": f"dig:mut:{idx}",
                        "baseVersion": 0,
                        "del": False,
                        "row": {
                            "_id": f"portal-dig-{idx}",
                            "_o": idx,
                            "title": f"Digest Idea {idx}",
                            "stage": "Идея",
                        },
                    }
                ),
            )

    sent_emails: list[dict[str, str]] = []

    async def failing_smtp(_mail: dict[str, str]) -> None:
        raise RuntimeError("SMTP connection refused")

    async def working_smtp(mail: dict[str, str]) -> None:
        sent_emails.append(mail)

    # 1. SMTP failure -> raises before recording email_notified
    async with session_maker() as s:
        with pytest.raises(RuntimeError):
            async with s.begin():
                await send_sunday_notifications(s, send_mail_fn=failing_smtp)

    async with session_maker() as s:
        notified = (await s.execute(select(EmailNotified))).scalars().all()
        assert len(notified) == 0

    # 2. Next run with working SMTP -> sends 1 batched email for 2 proposals and records both
    async with session_maker() as s:
        async with s.begin():
            status_msg = await send_sunday_notifications(s, send_mail_fn=working_smtp)
            assert "Sent: 2" in status_msg

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "hq@sunday.kz"
    assert "portal-dig-1" in sent_emails[0]["body"]
    assert "portal-dig-2" in sent_emails[0]["body"]

    # 3. Third run -> No new proposals
    async with session_maker() as s:
        async with s.begin():
            status_again = await send_sunday_notifications(s, send_mail_fn=working_smtp)
            assert status_again == "No new proposals"


@pytest.mark.asyncio
async def test_openrouter_ai_proxy_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify §5.3 OpenRouter AI proxy: no_key, bad_request, 60k limit, and successful proxy."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Missing OPENROUTER_KEY -> no_key
        monkeypatch.setattr(settings, "OPENROUTER_KEY", "")
        r1 = await client.post(
            "/", content=json.dumps({"ai": {"messages": [{"role": "user", "content": "Hi"}]}})
        )
        assert r1.json() == {"ok": False, "error": "no_key"}

        # 2. Empty messages -> bad_request
        monkeypatch.setattr(settings, "OPENROUTER_KEY", "sk-or-test-key")
        r2 = await client.post("/", content=json.dumps({"ai": {"messages": []}}))
        assert r2.json() == {"ok": False, "error": "bad_request"}

        # 3. Total chars > 60,000 -> bad_request
        r3 = await client.post(
            "/",
            content=json.dumps(
                {"ai": {"messages": [{"role": "user", "content": "A" * 60005}]}}
            ),
        )
        assert r3.json() == {"ok": False, "error": "bad_request"}

        # 4. Valid request mocked
        from backend import app as app_mod

        async def fake_proxy(_req: Any) -> dict[str, Any]:
            return {"ok": True, "content": "Generated 5W1H Passport"}

        monkeypatch.setattr(app_mod, "handle_ai_proxy", fake_proxy)
        r4 = await client.post(
            "/",
            content=json.dumps({"ai": {"messages": [{"role": "user", "content": "Help"}]}}),
        )
        assert r4.json() == {"ok": True, "content": "Generated 5W1H Passport"}
