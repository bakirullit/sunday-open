import json
from typing import Any
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ai import handle_ai_proxy
from backend.config import settings
from backend.database import get_session
from backend.sync import apply_operation, propose_operation, read_all_data

app = FastAPI(title="Sunday Open Backend", version="1.0.0")

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
async def do_get(session: AsyncSession = Depends(get_session)) -> Response:
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

    # OpenRouter AI helper proxy bypasses locking and moderation
    if isinstance(pre, dict) and "ai" in pre:
        ai_res = await handle_ai_proxy(pre.get("ai"))
        return json_response(ai_res)

    protocol = settings.protocol_version
    if not isinstance(pre, dict) or pre.get("protocol") != protocol:
        return json_response(
            {"protocol": protocol, "ok": False, "error": "upgrade_required"}
        )

    try:
        async with session.begin():
            if protocol == 4:
                result = await propose_operation(session, pre)
            else:
                result = await apply_operation(session, pre)
        return json_response(result)
    except Exception as exc:
        return json_response({"protocol": protocol, "ok": False, "error": str(exc)})
