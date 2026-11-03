from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any, AsyncGenerator
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ai import handle_ai_proxy
from backend.bot import (
    generate_qr_png_bytes,
    handle_mini_app_api,
    handle_tg_webhook,
    verify_init_data,
)
from backend.config import settings
from backend.database import get_session
from backend.models import Proposal
from backend.moderation import (
    compute_proposal_diff,
    create_admin_token,
    get_kv,
    notify_moderators_on_propose,
    render_admin_html,
    render_admin_login_html,
    review_proposal,
    send_email_async,
    verify_admin_token,
    verify_moderator_email,
)
from backend.scheduler import create_scheduler
from backend.sync import apply_operation, propose_operation, read_all_data


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    scheduler = create_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Sunday Open Backend", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def json_response(payload: dict[str, Any]) -> Response:
    """Return HTTP 200 JSON response with CORS headers matching Apps Script sundayJson_."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return Response(
        content=body,
        status_code=200,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/")
async def do_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    accept = request.headers.get("accept", "")
    if "text/html" in accept and not accept.startswith("*/*"):
        idx_path = Path("index.html")
        if idx_path.is_file():
            return FileResponse("index.html", media_type="text/html")
    protocol = settings.protocol_version
    try:
        payload = await read_all_data(session)
        return json_response(payload)
    except Exception as exc:
        return json_response({"protocol": protocol, "ok": False, "error": str(exc)})


@app.post("/")
async def do_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else "null"
    pre: Any = None
    try:
        pre = json.loads(raw_text)
    except Exception:
        pre = None

    # 1. Telegram Webhook (?tg=<TG_SECRET>)
    tg_secret_param = request.query_params.get("tg")
    if tg_secret_param is not None:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        try:
            async with session.begin():
                await handle_tg_webhook(
                    session=session,
                    secret_param=tg_secret_param,
                    header_secret=header_secret,
                    update=pre,
                )
        except Exception:
            pass
        return PlainTextResponse(
            content="ok",
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # 2. OpenRouter AI helper proxy bypasses locking and moderation
    if isinstance(pre, dict) and "ai" in pre:
        ai_res = await handle_ai_proxy(pre.get("ai"))
        return json_response(ai_res)

    # 3. Telegram Mini App API (me / reg / rsvp)
    if isinstance(pre, dict) and any(k in pre for k in ("me", "reg", "rsvp")):
        try:
            async with session.begin():
                mini_res = await handle_mini_app_api(session, pre)
            return json_response(mini_res)
        except Exception:
            return json_response({"ok": False, "error": "busy"})

    # 4. Sync operation (protocol 3 or 4)
    protocol = settings.protocol_version
    if not isinstance(pre, dict) or pre.get("protocol") != protocol:
        return json_response(
            {"protocol": protocol, "ok": False, "error": "upgrade_required"}
        )

    try:
        async with session.begin():
            if protocol == 4:
                result = await propose_operation(
                    session,
                    pre,
                    verify_tg_fn=verify_init_data,
                    notify_mods_fn=notify_moderators_on_propose,
                )
            else:
                result = await apply_operation(session, pre)
        return json_response(result)
    except Exception as exc:
        return json_response({"protocol": protocol, "ok": False, "error": str(exc)})


@app.get("/qr/{code}.png")
async def qr_code_image(
    code: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot_username = await get_kv(session, "TG_BOT_USERNAME", "")
    png_bytes = generate_qr_png_bytes(code, bot_username)
    return Response(content=png_bytes, media_type="image/png")


def _resolve_admin_email(request: Request, token: str | None = None) -> str | None:
    candidate = (
        token
        or request.query_params.get("token")
        or request.cookies.get("sunday_admin_token")
        or request.headers.get("X-Admin-Token")
    )
    return verify_admin_token(candidate)


@app.post("/admin/login")
async def admin_login(request: Request, email: str = Form(...)) -> Response:
    try:
        clean = verify_moderator_email(email)
    except PermissionError as exc:
        if "text/html" in (request.headers.get("accept") or ""):
            return HTMLResponse(
                content=render_admin_login_html(error=str(exc)), status_code=403
            )
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    token = create_admin_token(clean)
    base = (settings.PUBLIC_BASE_URL or "http://localhost:8832").rstrip("/")
    magic_link = f"/admin?token={token}"
    if settings.SMTP_HOST:
        try:
            await send_email_async(
                clean,
                "Sunday Open: вход в панель модератора",
                f"Ссылка для входа в очередь одобрения:\n{base}{magic_link}",
            )
        except Exception:
            pass

    if "application/json" in (request.headers.get("accept") or ""):
        return json_response(
            {"ok": True, "email": clean, "magic_link": f"{base}{magic_link}"}
        )

    resp = RedirectResponse(url=magic_link, status_code=303)
    resp.set_cookie(
        "sunday_admin_token", token, httponly=True, samesite="lax", max_age=86400
    )
    return resp


@app.get("/admin")
async def admin_queue(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    email = _resolve_admin_email(request)
    if not email:
        if "text/html" in (request.headers.get("accept") or ""):
            return HTMLResponse(
                content=render_admin_login_html(), status_code=403
            )
        raise HTTPException(
            status_code=403,
            detail="Only a configured moderator with a verified Google account can review changes",
        )

    token = (
        request.query_params.get("token")
        or request.cookies.get("sunday_admin_token")
        or create_admin_token(email)
    )
    stmt = (
        select(Proposal)
        .where(Proposal.status == "pending")
        .order_by(Proposal.created_at.asc())
    )
    proposals = list((await session.execute(stmt)).scalars().all())

    if "application/json" in (request.headers.get("accept") or ""):
        return json_response(
            {
                "ok": True,
                "moderator": email,
                "proposals": [
                    {
                        "mutationId": p.mutation_id,
                        "table": p.tbl,
                        "id": p.id,
                        "baseVersion": p.base_version,
                        "isDelete": p.is_delete,
                        "status": p.status,
                        "diff": compute_proposal_diff(p),
                    }
                    for p in proposals
                ],
            }
        )

    html_content = render_admin_html(email, proposals, token)
    resp = HTMLResponse(content=html_content, status_code=200)
    resp.set_cookie(
        "sunday_admin_token", token, httponly=True, samesite="lax", max_age=86400
    )
    return resp


@app.post("/admin/review")
async def admin_review_action(
    request: Request,
    mutation_id: str = Form(...),
    action: str = Form(...),
    token: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    email = _resolve_admin_email(request, token=token)
    if not email:
        raise HTTPException(
            status_code=403,
            detail="Only a configured moderator with a verified Google account can review changes",
        )
    approve = action == "approve"
    try:
        async with session.begin():
            res = await review_proposal(
                session=session,
                proposal_ref=mutation_id,
                approve=approve,
                reviewer=email,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if "application/json" in (request.headers.get("accept") or ""):
        return json_response(
            {
                "ok": True,
                "mutationId": mutation_id,
                "status": res["status"],
                "changed": res["changed"],
                "reviewedBy": email,
            }
        )

    active_token = token or create_admin_token(email)
    return RedirectResponse(url=f"/admin?token={active_token}", status_code=303)


@app.get("/workspace")
async def workspace_page() -> Response:
    idx_path = Path("index.html")
    if idx_path.is_file():
        return FileResponse(idx_path, media_type="text/html")
    return HTMLResponse("<h1>Workspace index.html not found</h1>", status_code=404)


@app.get("/app")
@app.get("/app/")
async def mini_app_page() -> Response:
    idx_path = Path("app/index.html")
    if idx_path.is_file():
        return FileResponse(idx_path, media_type="text/html")
    return HTMLResponse("<h1>Mini App index.html not found</h1>", status_code=404)


@app.get("/{static_file:path}")
async def serve_static_root(static_file: str) -> Response:
    p = Path(static_file)
    if p.is_file() and p.suffix in (
        ".js",
        ".css",
        ".html",
        ".png",
        ".svg",
        ".json",
        ".woff2",
        ".ico",
    ):
        return FileResponse(p)
    raise HTTPException(status_code=404, detail="Not found")
