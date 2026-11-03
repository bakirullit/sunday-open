from typing import Any
import httpx
from backend.config import settings
from backend.sync import js_json_stringify

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openrouter/free"


async def handle_ai_proxy(
    req: Any,
    api_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Forward chat completion requests to OpenRouter matching sundayAi_ in Code.gs."""
    try:
        key = api_key if api_key is not None else settings.OPENROUTER_KEY
        if not key:
            return {"ok": False, "error": "no_key"}

        if not isinstance(req, dict) or not isinstance(req.get("messages"), list):
            return {"ok": False, "error": "bad_request"}

        messages = req["messages"][-24:]
        if not messages or len(js_json_stringify(messages)) > 60000:
            return {"ok": False, "error": "bad_request"}

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "Sunday Open",
        }
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
        }

        if http_client is not None:
            resp = await http_client.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=30.0
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    OPENROUTER_URL, headers=headers, json=payload
                )

        data = resp.json()
        if data.get("error") or not data.get("choices"):
            err_obj = data.get("error")
            err_msg = (
                err_obj.get("message")
                if isinstance(err_obj, dict) and err_obj.get("message")
                else "ai_error"
            )
            return {"ok": False, "error": err_msg}

        content = data["choices"][0]["message"]["content"]
        return {"ok": True, "content": content}
    except Exception as err:
        return {"ok": False, "error": str(err)}
