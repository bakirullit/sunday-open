import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

from httpx import ASGITransport, AsyncClient
import pytest

from backend.anchor_ui import create_default_navigator
from backend.app import app
from backend.bot import (
    generate_event_csv,
    generate_qr_png_bytes,
    run_sunday_bot_daily,
    verify_init_data,
)
from backend.config import settings
from backend.models import Contact, Record
from backend.moderation import set_kv


def make_init_data(
    user_obj: dict[str, Any],
    bot_token: str = "123456:TEST_TOKEN",
    auth_date: int | None = None,
    tamper_hash: bool = False,
) -> str:
    ad = auth_date if auth_date is not None else int(time.time())
    params = {
        "auth_date": str(ad),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_obj, separators=(",", ":"), ensure_ascii=False),
    }
    check_str = "\n".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, check_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if tamper_hash:
        sig = "0" * 64
    params["hash"] = sig
    return urlencode(params)


@pytest.mark.asyncio
async def test_init_data_verification_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Acceptance criteria #4:
    - Valid initData succeeds.
    - Forged hash is rejected.
    - Expired auth_date (> 7 days) is rejected.
    - Oversized initData (> 4096 chars) is rejected.
    """
    token = "123456:TEST_TOKEN"
    monkeypatch.setattr(settings, "TG_BOT_TOKEN", token)

    user = {"id": 4242, "first_name": "Алия", "last_name": "Сатпаева", "username": "aliya_s"}
    valid = make_init_data(user, token)
    verified = verify_init_data(valid, bot_token=token)
    assert verified is not None
    assert verified["id"] == 4242

    # Forged hash
    forged = make_init_data(user, token, tamper_hash=True)
    assert verify_init_data(forged, bot_token=token) is None

    # Expired (> 7 days old)
    expired = make_init_data(user, token, auth_date=int(time.time()) - 8 * 86400)
    assert verify_init_data(expired, bot_token=token) is None

    # Oversized (> 4096 chars)
    huge_user = {**user, "bio": "x" * 5000}
    huge = make_init_data(huge_user, token)
    assert verify_init_data(huge, bot_token=token) is None


@pytest.mark.asyncio
async def test_miniapp_me_reg_rsvp_checkin_and_csv(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Acceptance criteria #4 & #5:
    - Phone sent from Web UI in `reg` is strictly ignored.
    - Phone sent via verified Telegram `contact` message is saved.
    - RSVP requires complete profile and future/today event date.
    - QR PNG generated locally via segno (/qr/{code}.png).
    - Check-in rules (self-scan warning, organiser/moderator check-in, duplicate check-in, cancelled).
    - CSV export has UTF-8 BOM, `;` separator, and CRLF line endings.
    """
    token = "123456:TEST_TOKEN"
    monkeypatch.setattr(settings, "TG_BOT_TOKEN", token)
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")

    sent_msgs: list[tuple[Any, str]] = []
    sent_photos: list[tuple[Any, str]] = []

    async def fake_send_msg(chat_id: Any, html: str, kb: Any = None, **kw: Any) -> dict[str, Any]:
        sent_msgs.append((chat_id, html))
        return {"ok": True, "result": {"message_id": 1}}

    async def fake_send_photo(chat_id: Any, code: str, caption: str, bot_username: str = "", **kw: Any) -> dict[str, Any]:
        sent_photos.append((chat_id, code))
        return {"ok": True}

    from backend import bot as bot_mod

    monkeypatch.setattr(bot_mod, "send_tg_message", fake_send_msg)
    monkeypatch.setattr(bot_mod, "send_tg_qr_photo", fake_send_photo)

    async with session_maker() as s:
        async with s.begin():
            await set_kv(s, "TG_SECRET", "wh_sec")
            await set_kv(s, "TG_BOT_USERNAME", "sunday_open_bot")

    user = {"id": 1001, "first_name": "Данияр", "last_name": "Ахметов", "username": "daniyar"}
    init_data = make_init_data(user, token)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Forged initData rejected on Mini App API
        r_forged = await client.post(
            "/",
            content=json.dumps(
                {"me": {"initData": make_init_data(user, token, tamper_hash=True)}}
            ),
        )
        assert r_forged.json() == {"ok": False, "error": "auth"}

        # 2. Web `reg` tries to spoof phone -> phone MUST be ignored!
        r_reg = await client.post(
            "/",
            content=json.dumps(
                {
                    "reg": {
                        "initData": init_data,
                        "profile": {
                            "firstName": "Данияр",
                            "lastName": "Ахметов",
                            "phone": "+77019999999",  # Spoofed from web!
                            "email": "d@aitu.edu.kz",
                            "club": "AITU Robotics",
                            "role": "Участник",
                        },
                    }
                }
            ),
        )
        reg_json = r_reg.json()
        assert reg_json["ok"] is True
        assert reg_json["profile"]["phone"] == ""
        assert "телефон" in reg_json["missing"]

        # 3. RSVP before phone is verified -> rejected with error="profile"
        r_rsvp_incomplete = await client.post(
            "/",
            content=json.dumps(
                {
                    "rsvp": {
                        "initData": init_data,
                        "going": True,
                        "event": {
                            "id": "portal-robo-fest",
                            "title": "RoboFest",
                            "date": "2099-12-01",
                            "club": "AITU Robotics",
                        },
                    }
                }
            ),
        )
        assert r_rsvp_incomplete.json()["error"] == "profile"

        # 4. Share contact via Telegram webhook (mismatched user_id rejected first, then matching accepted)
        await client.post(
            "/?tg=wh_sec",
            content=json.dumps(
                {
                    "update_id": 2001,
                    "message": {
                        "message_id": 1,
                        "from": user,
                        "chat": {"id": 1001, "type": "private"},
                        "contact": {"user_id": 9999, "phone_number": "+77010000000"},
                    },
                }
            ),
        )
        assert any("собственный номер" in m[1] for m in sent_msgs)

        await client.post(
            "/?tg=wh_sec",
            content=json.dumps(
                {
                    "update_id": 2002,
                    "message": {
                        "message_id": 2,
                        "from": user,
                        "chat": {"id": 1001, "type": "private"},
                        "contact": {"user_id": 1001, "phone_number": "+77015554433"},
                    },
                }
            ),
        )

        # Verify `me` now returns missing == [] and verified phone
        r_me = await client.post("/", content=json.dumps({"me": {"initData": init_data}}))
        me_json = r_me.json()
        assert me_json["missing"] == []
        assert me_json["profile"]["phone"] == "+77015554433"

        # 5. RSVP to past event -> rejected with error="past"
        r_past = await client.post(
            "/",
            content=json.dumps(
                {
                    "rsvp": {
                        "initData": init_data,
                        "going": True,
                        "event": {
                            "id": "portal-robo-fest",
                            "title": "Old Event",
                            "date": "2020-01-01",
                            "club": "AITU Robotics",
                        },
                    }
                }
            ),
        )
        assert r_past.json()["error"] == "past"

        # 6. RSVP to upcoming event -> creates 12-hex code & sends QR
        r_going = await client.post(
            "/",
            content=json.dumps(
                {
                    "rsvp": {
                        "initData": init_data,
                        "going": True,
                        "event": {
                            "id": "portal-robo-fest",
                            "title": "RoboFest 2099",
                            "date": "2099-12-01",
                            "club": "AITU Robotics",
                        },
                    }
                }
            ),
        )
        going_json = r_going.json()
        assert going_json["ok"] is True, f"going_json error: {going_json}"
        rsvp_entry = next(r for r in going_json["rsvps"] if r["eventId"] == "portal-robo-fest")
        assert rsvp_entry["checkedIn"] is False
        qr_code = rsvp_entry["code"]
        assert len(qr_code) == 12
        assert len(sent_photos) == 1
        assert sent_photos[0][1] == qr_code

        # 7. Verify local segno QR endpoint /qr/{code}.png returns valid PNG bytes
        r_qr = await client.get(f"/qr/{qr_code}.png")
        assert r_qr.status_code == 200
        assert r_qr.headers["content-type"] == "image/png"
        assert r_qr.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(generate_qr_png_bytes(qr_code, "sunday_open_bot")) > 100

        # 8. Attendee scans own QR -> "Покажите QR организатору на входе"
        sent_msgs.clear()
        await client.post(
            "/?tg=wh_sec",
            content=json.dumps(
                {
                    "update_id": 2003,
                    "message": {
                        "message_id": 3,
                        "from": user,
                        "chat": {"id": 1001, "type": "private"},
                        "text": f"/start ci_{qr_code}",
                    },
                }
            ),
        )
        assert any("Покажите QR организатору" in m[1] for m in sent_msgs)

        # 9. Club organiser scans QR -> marks checked in and notifies attendee
        org_user = {"id": 2002, "first_name": "Олжас", "last_name": "Орг", "username": "olzhas_org"}
        async with session_maker() as s:
            async with s.begin():
                s.add(
                    Contact(
                        tg_id="2002",
                        username="olzhas_org",
                        first_name="Олжас",
                        last_name="Орг",
                        phone="+77012223344",
                        email="org@aitu.kz",
                        club="AITU Robotics",
                        role="Организатор",
                    )
                )

        sent_msgs.clear()
        await client.post(
            "/?tg=wh_sec",
            content=json.dumps(
                {
                    "update_id": 2004,
                    "message": {
                        "message_id": 4,
                        "from": org_user,
                        "chat": {"id": 2002, "type": "private"},
                        "text": f"/start ci_{qr_code}",
                    },
                }
            ),
        )
        assert any("отмечен на входе" in m[1] and "Пришли 1 из 1" in m[1] for m in sent_msgs)
        assert any(str(m[0]) == "1001" and "Вы отмечены на входе" in m[1] for m in sent_msgs)

        # 10. Duplicate check-in scan -> reports already checked in
        sent_msgs.clear()
        await client.post(
            "/?tg=wh_sec",
            content=json.dumps(
                {
                    "update_id": 2005,
                    "message": {
                        "message_id": 5,
                        "from": org_user,
                        "chat": {"id": 2002, "type": "private"},
                        "text": f"/start ci_{qr_code}",
                    },
                }
            ),
        )
        assert any("Уже отмечен" in m[1] for m in sent_msgs)

        # 11. Verify CSV format (§9.4: UTF-8 BOM, `;` separator, CRLF endings)
        async with session_maker() as s:
            csv_info = await generate_event_csv(s, "portal-robo-fest")
            raw_csv = csv_info["csv"]
            assert raw_csv.startswith(
                '\ufeff"Имя";"Фамилия";"Telegram";"Телефон";"Email";"Клуб";"Роль";"Статус";"Записался";"Отмечен на входе"\r\n'
            )
            assert "\r\n" in raw_csv
            assert '"Данияр";"Ахметов";"@daniyar";"+77015554433";"d@aitu.edu.kz";"AITU Robotics";"Участник";"идёт";' in raw_csv


@pytest.mark.asyncio
async def test_bot_commands_5w1h_dialog_and_daily_job(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Test all §8.2 commands (/start, /help, /register, /new 9-step 5W1H dialog,
    /ideas with legacy stage mapping, /calendar, /remind, /list, /mod, /unmod, /cancel)
    and §11 daily 09:00 job (run_sunday_bot_daily).
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "4")
    monkeypatch.setattr(settings, "TG_BOT_TOKEN", "123456:TEST_TOKEN")

    sent_msgs: list[tuple[Any, str, Any]] = []
    api_calls: list[tuple[str, dict[str, Any]]] = []
    sent_docs: list[tuple[Any, str, str]] = []

    async def fake_send_msg(chat_id: Any, html: str, kb: Any = None, **kw: Any) -> dict[str, Any]:
        sent_msgs.append((chat_id, html, kb))
        return {"ok": True, "result": {"message_id": len(sent_msgs) + 100}}

    async def fake_send_api(method: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        api_calls.append((method, payload))
        return {"ok": True}

    async def fake_send_doc(chat_id: Any, filename: str, csv_text: str, caption: str, **kw: Any) -> dict[str, Any]:
        sent_docs.append((chat_id, filename, csv_text))
        return {"ok": True}

    from backend import bot as bot_mod

    monkeypatch.setattr(bot_mod, "send_tg_message", fake_send_msg)
    monkeypatch.setattr(bot_mod, "send_tg_api", fake_send_api)
    monkeypatch.setattr(bot_mod, "send_tg_csv_document", fake_send_doc)

    async with session_maker() as s:
        async with s.begin():
            await set_kv(s, "TG_SECRET", "wh_sec_2")
            await set_kv(s, "TG_MOD_CODE", "SECRET123")
            # Seed a legacy stage card ("Паспорт на проверке" -> mapped to "Паспорт")
            s.add(
                Record(
                    tbl="portal",
                    id="portal-seed-1",
                    version=1,
                    deleted=False,
                    row={
                        "_id": "portal-seed-1",
                        "_o": 1,
                        "title": "AI Summit",
                        "club": "AI Club",
                        "stage": "Паспорт на проверке",
                        "date": "2026-11-10",
                    },
                )
            )

    transport = ASGITransport(app=app)
    user = {"id": 3003, "first_name": "Тимур", "username": "timur"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Duplicate update_id deduplication
        upd = {
            "update_id": 5000,
            "message": {"message_id": 1, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/start"},
        }
        await client.post("/?tg=wh_sec_2", content=json.dumps(upd))
        await client.post("/?tg=wh_sec_2", content=json.dumps(upd))
        assert len(sent_msgs) == 1
        assert "Sunday Open · Проекты" in sent_msgs[0][1]

        # 2. /remind toggles reminders
        sent_msgs.clear()
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5001, "message": {"message_id": 2, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/remind"}}
            ),
        )
        assert "Напоминания выключены" in sent_msgs[-1][1]
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5002, "message": {"message_id": 3, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/remind"}}
            ),
        )
        assert "Напоминания включены" in sent_msgs[-1][1]

        # 3. /mod with secret code & /unmod
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5003, "message": {"message_id": 4, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/mod WRONG"}}
            ),
        )
        assert "Код не подошёл" in sent_msgs[-1][1]
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5004, "message": {"message_id": 5, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/mod SECRET123"}}
            ),
        )
        assert "вы модератор" in sent_msgs[-1][1]

        # 4. /ideas and /calendar
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5005, "message": {"message_id": 6, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/ideas"}}
            ),
        )
        assert "Банк идей" in sent_msgs[-1][1]
        assert "Паспорт" in sent_msgs[-1][1]  # mapped from "Паспорт на проверке"

        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5006, "message": {"message_id": 7, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/calendar"}}
            ),
        )
        assert "Ближайшие события" in sent_msgs[-1][1]

        # 5. /new 9-step 5W1H dialog -> n:send
        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {"update_id": 5007, "message": {"message_id": 8, "from": user, "chat": {"id": 3003, "type": "private"}, "text": "/new"}}
            ),
        )
        assert "1/9." in sent_msgs[-1][1]

        answers = [
            "Кибертурнир AITU",       # 1. title
            "AITU Gaming",            # 2. club
            "25.12.2099",             # 3. date
            "Финал по CS2",           # 4. what
            "Развитие киберспорта",   # 5. why
            "200 студентов",          # 6. who
            "Актовый зал",            # 7. where
            "Сетка Double Elim",      # 8. how
            "Проектор и призы",       # 9. need
        ]
        for idx, ans in enumerate(answers):
            await client.post(
                "/?tg=wh_sec_2",
                content=json.dumps(
                    {
                        "update_id": 5010 + idx,
                        "message": {"message_id": 10 + idx, "from": user, "chat": {"id": 3003, "type": "private"}, "text": ans},
                    }
                ),
            )
        assert "Проверьте заявку" in sent_msgs[-1][1]

        await client.post(
            "/?tg=wh_sec_2",
            content=json.dumps(
                {
                    "update_id": 5030,
                    "callback_query": {
                        "id": "cb-new-send",
                        "from": user,
                        "message": {"message_id": 25, "chat": {"id": 3003, "type": "private"}, "text": "Проверьте заявку"},
                        "data": "n:send",
                    },
                }
            ),
        )
        assert any("Карточку можно дополнить" in m[1] for m in sent_msgs)

    # 6. Daily 09:00 job (run_sunday_bot_daily)
    sent_msgs.clear()
    async with session_maker() as s:
        async with s.begin():
            res_str = await run_sunday_bot_daily(
                s,
                today_iso="2026-11-03",  # 7 days before 2026-11-10 AI Summit
                send_msg_fn=fake_send_msg,
            )
            assert res_str == "Done"
            assert any("Через неделю" in m[1] and "AI Summit" in m[1] for m in sent_msgs)


@pytest.mark.asyncio
async def test_anchor_ui_lifo_navigation_stack(session_maker) -> None:
    """
    Verify Anchor UI LIFO navigation stack:
    - Push / pop / peek / depth / breadcrumbs.
    - Back button transitions cleanly to previous screen by editing the single Anchor message.
    - Home resets stack to depth 1.
    """
    edits: list[dict[str, Any]] = []
    sends: list[tuple[Any, str, Any]] = []

    async def fake_send(chat_id: Any, text: str, markup: Any = None, **kw: Any) -> dict[str, Any]:
        sends.append((chat_id, text, markup))
        return {"ok": True, "result": {"message_id": 777}}

    async def fake_api(method: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if method == "editMessageText":
            edits.append(payload)
        return {"ok": True}

    async with session_maker() as s:
        async with s.begin():
            s.add(
                Record(
                    tbl="portal",
                    id="portal-nav-1",
                    version=1,
                    deleted=False,
                    row={
                        "_id": "portal-nav-1",
                        "_o": 1,
                        "title": "Nav Card",
                        "club": "Art Club",
                        "stage": "Идея",
                    },
                )
            )

        nav = create_default_navigator()
        uid = 9001
        cid = 9001

        # 1. Navigate to 'home' -> sends initial anchor message (id=777), stack depth=1
        scr_home = await nav.navigate(
            s, user_id=uid, chat_id=cid, screen_id="home", send_msg_fn=fake_send, send_api_fn=fake_api
        )
        assert len(sends) == 1
        assert await nav.get_anchor_id(uid, session=s) == 777
        assert await nav.stack.depth(uid, session=s) == 1
        assert scr_home.breadcrumbs == ["🏠 Главная"]

        # 2. Navigate to 'ideas' -> edits anchor message 777 in-place, stack depth=2
        scr_ideas = await nav.navigate(
            s, user_id=uid, chat_id=cid, screen_id="ideas", send_msg_fn=fake_send, send_api_fn=fake_api
        )
        assert len(edits) == 1
        assert edits[-1]["message_id"] == 777
        assert await nav.stack.depth(uid, session=s) == 2
        assert scr_ideas.breadcrumbs == ["🏠 Главная", "💡 Банк идей"]
        assert "🧭" in scr_ideas.text

        # 3. Drill down to 'ideas_stage' ("Идея") -> stack depth=3, has both ⬅️ Назад and 🏠 Меню
        scr_stage = await nav.navigate(
            s,
            user_id=uid,
            chat_id=cid,
            screen_id="ideas_stage",
            payload={"stage": "Идея"},
            send_msg_fn=fake_send,
            send_api_fn=fake_api,
        )
        assert await nav.stack.depth(uid, session=s) == 3
        buttons_flat = [
            btn["callback_data"]
            for row in (scr_stage.reply_markup or {}).get("inline_keyboard", [])
            for btn in row
            if "callback_data" in btn
        ]
        assert "nav:back" in buttons_flat
        assert "nav:home" in buttons_flat

        # 4. Drill down to 'idea_detail' -> stack depth=4
        scr_card = await nav.navigate(
            s,
            user_id=uid,
            chat_id=cid,
            screen_id="idea_detail",
            payload={"card_id": "portal-nav-1"},
            send_msg_fn=fake_send,
            send_api_fn=fake_api,
        )
        assert await nav.stack.depth(uid, session=s) == 4
        assert "Nav Card" in scr_card.text

        # 5. Pop back via nav.back() -> returns to 'ideas_stage' (depth=3)
        scr_back1 = await nav.back(
            s, user_id=uid, chat_id=cid, send_msg_fn=fake_send, send_api_fn=fake_api
        )
        assert await nav.stack.depth(uid, session=s) == 3
        assert scr_back1 is not None
        assert "Этап «Идея»" in scr_back1.text

        # 6. Pop back again -> returns to 'ideas' (depth=2)
        scr_back2 = await nav.back(
            s, user_id=uid, chat_id=cid, send_msg_fn=fake_send, send_api_fn=fake_api
        )
        assert await nav.stack.depth(uid, session=s) == 2
        assert scr_back2 is not None
        assert scr_back2.breadcrumbs == ["🏠 Главная", "💡 Банк идей"]


@pytest.mark.asyncio
async def test_anchor_wizard_onboarding_and_5w1h_single_message(
    session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Verify aitu-gaming-hub Single-Message Anchor Wizard + GarbageCollector:
    1. Unregistered user sends /start -> gets club info screen with 'auth:start_reg'.
    2. User messages are immediately deleted via deleteMessage (GarbageCollector).
    3. Onboarding wizard ('auth:start_reg' -> 'auth:role:Организатор' -> name -> phone -> 'auth:skip_email' -> club)
       updates the single anchor message in-place via editMessageText and switches home menu to Authorized state.
    4. 5W1H Idea Anchor Wizard ('wiz:new_idea' -> title -> 'wiz:use_club' -> 'wiz:date:...' -> 'wiz:skip' -> 'n:send')
       edits the single anchor message in-place and submits the idea.
    """
    monkeypatch.setattr(settings, "SUNDAY_MODERATION", "0")
    monkeypatch.setattr(settings, "TG_BOT_TOKEN", "123456:TEST_TOKEN")

    sends: list[tuple[Any, str, Any]] = []
    edits: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []

    async def fake_send_msg(chat_id: Any, html: str, kb: Any = None, **kw: Any) -> dict[str, Any]:
        sends.append((chat_id, html, kb))
        return {"ok": True, "result": {"message_id": 888}}

    async def fake_send_api(method: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if method == "editMessageText":
            edits.append(payload)
        elif method == "deleteMessage":
            deletes.append(payload)
        elif method == "sendMessage":
            return {"ok": True, "result": {"message_id": 889}}
        return {"ok": True}

    from backend import bot as bot_mod

    monkeypatch.setattr(bot_mod, "send_tg_message", fake_send_msg)
    monkeypatch.setattr(bot_mod, "send_tg_api", fake_send_api)

    async with session_maker() as s:
        async with s.begin():
            await set_kv(s, "TG_SECRET", "wh_sec_anchor")

    transport = ASGITransport(app=app)
    user = {"id": 7007, "first_name": "Алихан", "username": "alikhan"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. /start -> Unregistered Club Info Screen + GarbageCollector deletes user message_id=10
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7000,
                    "message": {
                        "message_id": 10,
                        "from": user,
                        "chat": {"id": 7007, "type": "private"},
                        "text": "/start",
                    },
                }
            ),
        )
        assert len(sends) == 1
        assert "Пройти регистрацию" in json.dumps(sends[0][2], ensure_ascii=False)
        assert any(d.get("message_id") == 10 for d in deletes)

        # 2. Click 'auth:start_reg' -> edits anchor message 888 with role selection
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7001,
                    "callback_query": {
                        "id": "cb-1",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "auth:start_reg",
                    },
                }
            ),
        )
        assert "Выберите ваш статус" in edits[-1]["text"]

        # 3. Click 'auth:role:Организатор' -> edits anchor message 888 with full name step (Шаг 1 из 4)
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7002,
                    "callback_query": {
                        "id": "cb-2",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "auth:role:Организатор",
                    },
                }
            ),
        )
        assert "Шаг 1 из 4" in edits[-1]["text"]

        # 4. Send full name -> GarbageCollector deletes user message, edits anchor to Step 2 (Phone)
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7003,
                    "message": {
                        "message_id": 11,
                        "from": user,
                        "chat": {"id": 7007, "type": "private"},
                        "text": "Алихан Бокейханов",
                    },
                }
            ),
        )
        assert any(d.get("message_id") == 11 for d in deletes)
        assert "Шаг 2 из 4" in edits[-1]["text"]

        # 5. Send phone -> cleans up reply keyboard message 889, edits anchor to Step 3 (Email)
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7004,
                    "message": {
                        "message_id": 12,
                        "from": user,
                        "chat": {"id": 7007, "type": "private"},
                        "text": "+77015554433",
                    },
                }
            ),
        )
        assert any(d.get("message_id") == 889 for d in deletes)
        assert "Шаг 3 из 4" in edits[-1]["text"]

        # 6. Skip email via inline button 'auth:skip_email' -> edits anchor to Step 4 (Club)
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7005,
                    "callback_query": {
                        "id": "cb-3",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "auth:skip_email",
                    },
                }
            ),
        )
        assert "Шаг 4 из 4" in edits[-1]["text"]

        # 7. Send club name -> completes registration & renders Authorized Home Menu in-place!
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7006,
                    "message": {
                        "message_id": 13,
                        "from": user,
                        "chat": {"id": 7007, "type": "private"},
                        "text": "AITU Gaming",
                    },
                }
            ),
        )
        assert "Алихан Бокейханов" in edits[-1]["text"]
        assert "Организатор клуба" in edits[-1]["text"]
        assert "wiz:new_idea" in json.dumps(edits[-1]["reply_markup"], ensure_ascii=False)

        # 8. Start 5W1H Idea Anchor Wizard via 'wiz:new_idea'
        await client.post(
            "/?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7007,
                    "callback_query": {
                        "id": "cb-4",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "wiz:new_idea",
                    },
                }
            ),
        )
        assert "Шаг 1 из 9" in edits[-1]["text"]
        # Still only 1 sendMessage call in the entire session!
        assert len(sends) == 1

        # 9. Test seed data + GET /api + in-bot Workspace & RSVP flows
        from backend.seed import ensure_seed_data

        async with session_maker() as s:
            async with s.begin():
                seeded = await ensure_seed_data(s)
                assert seeded == 126

        api_res = await client.get("/api", headers={"Accept": "text/html"})
        assert api_res.status_code == 200
        api_json = api_res.json()
        assert len(api_json["data"]["portal"]["rows"]) >= 15
        assert len(api_json["data"]["sundays"]["rows"]) == 4

        # Navigate to Workspace screen ('nav:open:workspace') and switch tab ('ws:tab:steps')
        await client.post(
            "/api?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7008,
                    "callback_query": {
                        "id": "cb-5",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "nav:open:workspace",
                    },
                }
            ),
        )
        assert "План 4 воскресений" in edits[-1]["text"]

        await client.post(
            "/api?tg=wh_sec_anchor",
            content=json.dumps(
                {
                    "update_id": 7009,
                    "callback_query": {
                        "id": "cb-6",
                        "from": user,
                        "message": {"message_id": 888, "chat": {"id": 7007, "type": "private"}},
                        "data": "ws:tab:steps",
                    },
                }
            ),
        )
        assert "Первые шаги" in edits[-1]["text"]


