import asyncio
import hashlib
import hmac
import html
import json
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable, Awaitable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models import Author, EmailNotified, KeyValue, Proposal
from backend.sync import (
    compute_operation_hash,
    resolve_proposal_decision,
    validate_operation,
    verify_stored_hash,
)

TG_LABELS: dict[str, str] = {
    "title": "Название",
    "club": "Клуб",
    "contact": "Контакт",
    "date": "Дата",
    "what": "What",
    "why": "Why",
    "who": "Who",
    "where": "Where",
    "how": "How",
    "need": "Что нужно от штаба",
    "stage": "Этап",
    "owner": "Owner",
    "deputy": "Замещающий",
    "goal": "Цель",
    "scope_in": "Что входит",
    "scope_out": "Что не входит",
    "timeline": "Сроки",
    "budget": "Смета",
    "funding": "Источники",
    "stakeholders": "Stakeholders",
    "resources": "Ресурсы",
    "volunteers": "Волонтёры",
    "risks": "Риски",
    "results": "KPI",
    "reach": "Охват",
    "breakeven": "Безубыточность",
    "committees": "Проектная группа",
    "ms": "Вехи",
}


def tg_esc(s: Any) -> str:
    text = "" if s is None else str(s)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def tg_cut(s: Any, limit: int) -> str:
    text = str(s or "").strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def tg_name(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    first_last = " ".join(
        filter(None, [user.get("first_name"), user.get("last_name")])
    ).strip()
    username = user.get("username")
    return (first_last or "Без имени") + (f" (@{username})" if username else "")


def tg_table_label(tbl: str) -> str:
    return "Портал проектов" if tbl == "portal" else f"Workspace · {tbl}"


def tg_date_fmt(iso_date: Any) -> str:
    s = str(iso_date or "")
    parts = s.split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return s


def tg_webapp_button(
    text: str,
    params: str = "",
    webapp_url: str | None = None,
) -> dict[str, Any] | None:
    base = webapp_url if webapp_url is not None else settings.TG_WEBAPP_URL
    if not base:
        return None
    sep = "?" if "?" not in base else "&"
    url = f"{base}{sep}{params}" if params else base
    return {"text": text, "web_app": {"url": url}}


def tg_webapp_kb(
    text: str,
    params: str = "",
    webapp_url: str | None = None,
) -> dict[str, Any] | None:
    btn = tg_webapp_button(text, params, webapp_url=webapp_url)
    return {"inline_keyboard": [[btn]]} if btn else None


async def get_kv(session: AsyncSession, key: str, default: str = "") -> str:
    row = (
        await session.execute(select(KeyValue).where(KeyValue.key == key))
    ).scalar_one_or_none()
    return row.value if row is not None else default


async def set_kv(session: AsyncSession, key: str, value: str) -> None:
    row = (
        await session.execute(select(KeyValue).where(KeyValue.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(KeyValue(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def get_tg_moderators(session: AsyncSession) -> list[dict[str, Any]]:
    raw = await get_kv(session, "TG_MODERATORS", "[]")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


async def is_tg_moderator(session: AsyncSession, user_id: int | str) -> bool:
    mods = await get_tg_moderators(session)
    return any(str(m.get("id")) == str(user_id) for m in mods)


_shared_tg_client: httpx.AsyncClient | None = None
_shared_tg_loop_id: int | None = None


def _get_shared_tg_client() -> httpx.AsyncClient:
    """Return a keep-alive pooled httpx.AsyncClient bound to the current event loop."""
    global _shared_tg_client, _shared_tg_loop_id
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None
    if (
        _shared_tg_client is None
        or _shared_tg_client.is_closed
        or _shared_tg_loop_id != loop_id
    ):
        _shared_tg_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=60.0,
            ),
        )
        _shared_tg_loop_id = loop_id
    return _shared_tg_client


async def send_tg_api(
    method: str,
    payload: dict[str, Any],
    bot_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    token = bot_token if bot_token is not None else settings.TG_BOT_TOKEN
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        client = http_client or _get_shared_tg_client()
        resp = await client.post(url, json=payload, timeout=15.0)
        return resp.json()
    except Exception:
        return None


async def send_tg_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    bot_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await send_tg_api(
        "sendMessage", payload, bot_token=bot_token, http_client=http_client
    )


def format_moderator_proposal_notification(
    op: dict[str, Any],
    before_row: dict[str, Any] | None,
    author: dict[str, Any] | None,
    proposal_ref: str,
    op_hash: str,
) -> tuple[str, dict[str, Any]]:
    """Build Telegram notification text and inline keyboard matching tgNotifyMods_ in Code.gs."""
    is_del = bool(op.get("del"))
    row = (before_row or {}) if is_del else (op.get("row") or {})
    kind = (
        "запрос на удаление"
        if is_del
        else ("изменение" if before_row is not None else "новая карточка")
    )
    lines = [
        f"<b>Правка на одобрение</b> · {tg_esc(tg_table_label(op['table']))} · {kind}",
        "",
        f"<b>{tg_esc(row.get('title') or row.get('name') or op['id'])}</b>",
    ]
    if row.get("club"):
        lines.append(f"Клуб: {tg_esc(row['club'])}")
    if row.get("date"):
        lines.append(f"Дата: {tg_esc(tg_date_fmt(row['date']))}")

    if before_row is not None and not is_del:
        merged_keys = list({**before_row, **row}.keys())
        changed_keys = [
            k
            for k in merged_keys
            if not k.startswith("_")
            and str(before_row.get(k) or "") != str(row.get(k) or "")
        ]
        if before_row.get("stage") != row.get("stage"):
            lines.append(
                f"Этап: {tg_esc(before_row.get('stage') or '—')} → <b>{tg_esc(row.get('stage') or '—')}</b>"
            )
        if changed_keys:
            labels = ", ".join(TG_LABELS.get(k, k) for k in changed_keys)
            lines.append(f"Изменено: {tg_esc(labels)}")
    elif row.get("stage"):
        lines.append(f"Этап: {tg_esc(row['stage'])}")

    if row.get("what") and not is_del:
        lines.append("")
        lines.append(tg_esc(tg_cut(row["what"], 500)))

    author_str = (
        tg_esc(tg_name(author))
        if author
        else "сайт, без входа через Telegram"
    )
    lines.append("")
    lines.append(f"Автор: {author_str}")

    ref_short = proposal_ref[:48]
    cb_suffix = f"{ref_short}:{op_hash[:10]}"
    kb = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"a:{cb_suffix}"},
                {"text": "❌ Отклонить", "callback_data": f"r:{cb_suffix}"},
            ]
        ]
    }
    return "\n".join(lines), kb


async def notify_moderators_on_propose(
    session: AsyncSession,
    op: dict[str, Any],
    before_row: dict[str, Any] | None,
    author: dict[str, Any] | None,
    proposal_ref: str,
    op_hash: str,
    send_fn: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    mods = await get_tg_moderators(session)
    if not mods:
        return
    if send_fn is None and not settings.TG_BOT_TOKEN:
        return
    text, kb = format_moderator_proposal_notification(
        op=op,
        before_row=before_row,
        author=author,
        proposal_ref=proposal_ref,
        op_hash=op_hash,
    )
    sender = send_fn or send_tg_message
    for mod in mods:
        mod_id = mod.get("id")
        if mod_id is not None:
            await sender(mod_id, text, kb)


async def notify_author_on_review(
    session: AsyncSession,
    proposal: Proposal,
    status: str,
    send_fn: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Notify the proposal's Telegram author when approved, rejected, or conflict matching tgNotifyAuthor_."""
    author_rec = (
        await session.execute(
            select(Author).where(Author.mutation_id == proposal.mutation_id)
        )
    ).scalar_one_or_none()
    if author_rec is None:
        return
    if send_fn is None and not settings.TG_BOT_TOKEN:
        return

    row = (proposal.before if proposal.is_delete else proposal.after) or {}
    title = f"<b>{tg_esc(row.get('title') or proposal.id)}</b>"
    if status == "approved":
        action_word = "удаление " if proposal.is_delete else "правку "
        text = f"✅ Штаб одобрил {action_word}{title}. Она уже видна всем в портале."
    elif status == "rejected":
        text = f"❌ Штаб отклонил правку {title}. Откройте карточку, исправьте и отправьте снова."
    elif status == "conflict":
        text = f"⚠️ Правку {title} не применили: карточку успели изменить. Откройте портал, проверьте новую версию и отправьте ещё раз."
    else:
        return

    kb = (
        tg_webapp_kb("Открыть карточку", f"sel={proposal.id}")
        if (proposal.tbl == "portal" and not proposal.is_delete)
        else None
    )
    sender = send_fn or send_tg_message
    await sender(author_rec.tg_id, text, kb)


def verify_moderator_email(email: str | None) -> str:
    """Verify that an email belongs to SUNDAY_MODERATOR_EMAILS matching sundayReviewUser_."""
    clean = (email or "").strip().lower()
    allowed = settings.moderator_emails
    if not clean or clean not in allowed:
        raise PermissionError(
            "Only a configured moderator with a verified Google account can review changes"
        )
    return clean


async def review_proposal(
    session: AsyncSession,
    proposal_ref: str,
    approve: bool,
    reviewer: str,
    hash_prefix: str | None = None,
    send_author_fn: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """
    Review a proposal by mutation_id (or prefix from callback data) under row lock.
    Matches sundayReviewRow_ in Code.gs.
    """
    stmt = (
        select(Proposal)
        .where(Proposal.mutation_id == proposal_ref)
        .with_for_update()
    )
    proposal = (await session.execute(stmt)).scalar_one_or_none()
    if proposal is None and len(proposal_ref) == 48:
        stmt_prefix = (
            select(Proposal)
            .where(Proposal.mutation_id.like(f"{proposal_ref}%"))
            .with_for_update()
        )
        proposal = (await session.execute(stmt_prefix)).scalar_one_or_none()

    if proposal is None:
        raise LookupError("Proposal not found")

    if hash_prefix is not None and proposal.hash[:10] != hash_prefix:
        raise LookupError("Proposal hash prefix mismatch")

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

    changed = False
    if proposal.status == "pending":
        proposal.status = "approving" if approve else "rejected"
        proposal.reviewed_at = datetime.now(timezone.utc)
        proposal.reviewed_by = reviewer
        proposal.reason = "" if approve else "Rejected by moderator"
        await session.flush()
        changed = True

    result = await resolve_proposal_decision(session, proposal)
    await session.flush()

    if changed:
        try:
            await notify_author_on_review(
                session, proposal, result["status"], send_fn=send_author_fn
            )
        except Exception:
            pass

    return {"status": result["status"], "changed": changed, "proposal": proposal}


# ===================== 5-Minute Email Digest (§7.4) =====================

def _send_smtp_email_sync(to_email: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        if settings.SMTP_TLS:
            try:
                server.starttls()
            except Exception:
                pass
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


async def send_email_async(to_email: str, subject: str, body: str) -> None:
    await asyncio.to_thread(_send_smtp_email_sync, to_email, subject, body)


def get_notification_recipient(actor_email: str | None = None) -> str:
    """Validate notification recipient matching sundayNotificationRecipient_ in Code.gs."""
    email = (settings.SUNDAY_NOTIFICATION_EMAIL or "").strip().lower()
    allowed = settings.moderator_emails
    effective = (actor_email or email).strip().lower()
    if not email or email != effective or email not in allowed:
        raise PermissionError("Run notifications as their configured moderator")
    return email


def build_admin_review_link() -> str:
    base = (settings.PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")
    return f"{base}/admin"


async def send_sunday_notifications(
    session: AsyncSession,
    send_mail_fn: Callable[[ dict[str, str] ], Awaitable[None] | None] | None = None,
    actor_email: str | None = None,
    mail_quota: int = 100,
) -> str:
    """
    Send 5-minute email digest for unnotified pending proposals matching sendSundayNotifications in Code.gs.
    """
    if not (settings.SUNDAY_NOTIFICATION_EMAIL or "").strip():
        return "Disabled"

    recipient = get_notification_recipient(actor_email=actor_email)
    if not settings.is_moderated:
        raise RuntimeError("Moderation is not enabled")

    notified_ids_result = await session.execute(select(EmailNotified.mutation_id))
    sent_set = set(notified_ids_result.scalars().all())

    props_result = await session.execute(
        select(Proposal).order_by(Proposal.created_at.asc())
    )
    all_proposals = props_result.scalars().all()
    pending = [
        p
        for p in all_proposals
        if p.status == "pending" and p.mutation_id not in sent_set
    ]
    if not pending:
        return "No new proposals"

    # Validate each pending proposal before sending digest
    for p in pending:
        op = {
            "protocol": 4,
            "mutationId": p.mutation_id,
            "table": p.tbl,
            "id": p.id,
            "baseVersion": p.base_version,
            "del": p.is_delete,
            "row": None if p.is_delete else p.after,
        }
        validate_operation(op)
        if not verify_stored_hash(op, p.hash):
            raise ValueError("Invalid proposal hash")

    if mail_quota < 1:
        return "Email quota exhausted; retry later"

    summary_lines = [
        f"• {p.tbl} / {p.id}{' — удаление' if p.is_delete else ' — изменение'}"
        for p in pending[:20]
    ]
    summary = "\n".join(summary_lines)
    more_suffix = "\nОстальные правки — в таблице." if len(pending) > 20 else ""
    review_link = build_admin_review_link()

    subject = f"Sunday Open: новые правки на одобрение — {len(pending)}"
    body = (
        f"Новых правок: {len(pending)}. Они ещё ожидают одобрения.\n\n"
        f"Открыть очередь:\n{review_link}\n\n"
        f"{summary}{more_suffix}\n\n"
        "Выберите одну строку со статусом pending, затем меню Sunday Open → Одобрить выбранную правку или Отклонить выбранную правку.\n"
        "К моменту открытия таблицы часть правок уже может быть рассмотрена."
    )

    if send_mail_fn is not None:
        res = send_mail_fn(
            {
                "to": recipient,
                "name": "Sunday Open",
                "subject": subject,
                "body": body,
            }
        )
        if asyncio.iscoroutine(res):
            await res
    else:
        await send_email_async(recipient, subject, body)

    now = datetime.now(timezone.utc)
    for p in pending:
        session.add(EmailNotified(mutation_id=p.mutation_id, notified_at=now))
    await session.flush()

    return f"Sent: {len(pending)}"


async def send_sunday_notification_test(
    actor_email: str,
    send_mail_fn: Callable[[dict[str, str]], Awaitable[None] | None] | None = None,
) -> str:
    """Send a test notification email without modifying any proposals matching sendSundayNotificationTest."""
    verify_moderator_email(actor_email)
    recipient = get_notification_recipient(actor_email=actor_email)
    review_link = build_admin_review_link()
    subject = "Sunday Open: проверка уведомлений"
    body = (
        "Это проверочное письмо. Уведомления о новых правках проверяются примерно раз в 5 минут.\n\n"
        f"Очередь одобрения:\n{review_link}\n\n"
        "Это письмо не создаёт правок и не меняет данные сайта."
    )
    if send_mail_fn is not None:
        res = send_mail_fn(
            {
                "to": recipient,
                "name": "Sunday Open",
                "subject": subject,
                "body": body,
            }
        )
        if asyncio.iscoroutine(res):
            await res
    else:
        await send_email_async(recipient, subject, body)
    return f"Test notification sent to {recipient}"


# ===================== Admin Web UI Token & Diff Rendering (§7.3) =====================

def create_admin_token(email: str, ttl_seconds: int = 86400) -> str:
    clean = verify_moderator_email(email)
    exp = int(time.time()) + ttl_seconds
    payload = f"{clean}|{exp}"
    sig = hmac.new(
        settings.ADMIN_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{sig}"


def verify_admin_token(token: str | None) -> str | None:
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    email, exp_str, sig = parts
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if time.time() > exp:
        return None
    expected_sig = hmac.new(
        settings.ADMIN_SECRET.encode("utf-8"),
        f"{email}|{exp_str}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        return verify_moderator_email(email)
    except PermissionError:
        return None


def compute_proposal_diff(proposal: Proposal) -> list[dict[str, str]]:
    """Compute human-readable before/after field diffs for a proposal."""
    before = proposal.before or {}
    after = ({} if proposal.is_delete else proposal.after) or {}
    keys = sorted({*before.keys(), *after.keys()})
    diffs: list[dict[str, str]] = []
    for k in keys:
        if k.startswith("_"):
            continue
        b_val = "" if before.get(k) is None else str(before.get(k))
        a_val = "" if after.get(k) is None else str(after.get(k))
        if b_val != a_val or proposal.is_delete:
            diffs.append(
                {
                    "field": TG_LABELS.get(k, k),
                    "key": k,
                    "before": b_val,
                    "after": "[УДАЛЕНО]" if proposal.is_delete else a_val,
                }
            )
    return diffs


def render_admin_login_html(error: str | None = None) -> str:
    err_html = (
        f"<div style='color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:8px;margin-bottom:16px;font-size:14px;'>"
        f"{html.escape(error)}</div>"
        if error
        else ""
    )
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'/>"
        "<title>Sunday Open · Вход для модераторов</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        "<style>"
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f4f6; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 16px; }"
        ".card { background: #ffffff; padding: 32px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1); width: 100%; max-width: 420px; box-sizing: border-box; }"
        "h1 { margin: 0 0 8px 0; font-size: 20px; color: #1e293b; font-weight: 700; }"
        "p { margin: 0 0 20px 0; color: #64748b; font-size: 14px; line-height: 1.5; }"
        "label { display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: #374151; }"
        "input[type=email] { width: 100%; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; box-sizing: border-box; outline: none; transition: border-color 0.2s; }"
        "input[type=email]:focus { border-color: #2563eb; }"
        "button { width: 100%; margin-top: 16px; background: #2563eb; color: #ffffff; border: 0; padding: 11px; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }"
        "button:hover { background: #1d4ed8; }"
        ".links { margin-top: 24px; padding-top: 16px; border-top: 1px solid #e5e7eb; display: flex; justify-content: space-between; font-size: 13px; }"
        ".links a { color: #2563eb; text-decoration: none; }"
        ".links a:hover { text-decoration: underline; }"
        "</style></head>"
        "<body>"
        "<div class='card'>"
        "<h1>Sunday Open</h1>"
        "<p>Вход в панель модератора для рассмотрения правок и предложений.</p>"
        f"{err_html}"
        "<form method='post' action='/admin/login'>"
        "<label for='email'>Email модератора</label>"
        "<input id='email' type='email' name='email' placeholder='moderator@example.com' required autofocus/>"
        "<button type='submit'>Войти в панель</button>"
        "</form>"
        "<div class='links'>"
        "<a href='/workspace'>📋 Workspace</a>"
        "<a href='/app'>📱 Портал</a>"
        "<a href='/docs'>📖 API Docs</a>"
        "</div>"
        "</div></body></html>"
    )


def render_admin_html(email: str, proposals: list[Proposal], token: str) -> str:
    rows_html: list[str] = []
    for p in proposals:
        diffs = compute_proposal_diff(p)
        diff_items = "".join(
            f"<tr><td><b>{html.escape(d['field'])}</b></td>"
            f"<td style='color:#b91c1c;background:#fef2f2;'>{html.escape(d['before'])}</td>"
            f"<td style='color:#15803d;background:#f0fdf4;'>{html.escape(d['after'])}</td></tr>"
            for d in diffs
        )
        diff_table = (
            f"<table border='1' cellpadding='6' style='border-collapse:collapse;width:100%;font-size:13px;'>"
            f"<thead><tr><th>Поле</th><th>Было (before)</th><th>Стало (after)</th></tr></thead>"
            f"<tbody>{diff_items}</tbody></table>"
            if diff_items
            else "<em>Нет изменённых текстовых полей</em>"
        )
        kind = "Удаление" if p.is_delete else ("Изменение" if p.before else "Новая запись")
        rows_html.append(
            f"<div style='border:1px solid #d1d5db;border-radius:8px;padding:16px;margin-bottom:16px;background:#fff;'>"
            f"<div style='margin-bottom:8px;'><b>{html.escape(p.tbl)} / {html.escape(p.id)}</b> · "
            f"<span>{kind}</span> · <small>v{p.base_version} → v{p.base_version + 1} · {html.escape(p.mutation_id)}</small></div>"
            f"{diff_table}"
            f"<div style='margin-top:12px;display:flex;gap:8px;'>"
            f"<form method='post' action='/admin/review' style='display:inline;'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'/>"
            f"<input type='hidden' name='mutation_id' value='{html.escape(p.mutation_id)}'/>"
            f"<input type='hidden' name='action' value='approve'/>"
            f"<button type='submit' style='background:#16a34a;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;'>✅ Одобрить</button>"
            f"</form>"
            f"<form method='post' action='/admin/review' style='display:inline;'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'/>"
            f"<input type='hidden' name='mutation_id' value='{html.escape(p.mutation_id)}'/>"
            f"<input type='hidden' name='action' value='reject'/>"
            f"<button type='submit' style='background:#dc2626;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;'>❌ Отклонить</button>"
            f"</form>"
            f"</div></div>"
        )
    queue_body = (
        "".join(rows_html)
        if rows_html
        else "<p>Очередь одобрения пуста (нет ожидающих правок).</p>"
    )
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'/>"
        "<title>Sunday Open · Модерация</title></head>"
        "<body style='font-family:sans-serif;max-width:960px;margin:24px auto;padding:0 16px;background:#f9fafb;'>"
        f"<h1>Sunday Open · Очередь одобрения</h1>"
        f"<p>Модератор: <b>{html.escape(email)}</b> · Ожидают решения: <b>{len(proposals)}</b></p>"
        f"{queue_body}</body></html>"
    )
