"""Дополнение из TGStat. Без TGSTAT_TOKEN запросы не делаются."""
from __future__ import annotations

import json
import re

from app.settings import settings

API_URL = "https://api.tgstat.ru/channels/get"
PAGE_URLS = (
    "https://tgstat.ru/chat/@{username}",
    "https://tgstat.ru/channel/@{username}",
)


async def enrich(username: str | None, fetch=None) -> dict | None:
    token = (settings.tgstat_token or "").strip()
    name = (username or "").lstrip("@").strip()
    if not token or not name:
        return None
    if fetch is None:
        fetch = _http_get
    try:
        status, body = await fetch(API_URL, {"token": token, "channelId": f"@{name}"})
    except Exception as exc:
        return {"ok": False, "error": f"tgstat недоступен: {exc}"}
    parsed = _from_api(status, body)
    if parsed.get("ok"):
        return parsed
    for template in PAGE_URLS:
        try:
            page_status, page_body = await fetch(template.format(username=name), None)
        except Exception:
            continue
        page = _from_page(page_status, page_body)
        if page.get("ok"):
            return page
    if parsed.get("error"):
        return parsed
    return {"ok": False, "error": "tgstat не отдал карточку канала"}


def _from_api(status: int, body: str) -> dict:
    if status != 200:
        return {"ok": False, "error": f"tgstat api {status}"}
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "tgstat api вернул не json"}
    if payload.get("status") != "ok":
        error = payload.get("error") or payload.get("message") or "tgstat api отказал"
        return {"ok": False, "error": str(error)}
    response = payload.get("response") or {}
    if not isinstance(response, dict):
        return {"ok": False, "error": "пустой ответ tgstat"}
    return {
        "ok": True,
        "source": "api",
        "participants": response.get("participants_count"),
        "title": response.get("title") or "",
        "about": response.get("about") or "",
        "peer_type": response.get("peer_type") or "",
        "ci_index": response.get("ci_index"),
    }


def _from_page(status: int, body: str) -> dict:
    if status != 200 or not body:
        return {"ok": False}
    match = re.search(r"([\d\s]{2,12})\s*участник", body.lower().replace("ё", "е"))
    if not match:
        return {"ok": False}
    digits = re.sub(r"\s+", "", match.group(1))
    try:
        participants = int(digits)
    except ValueError:
        return {"ok": False}
    return {"ok": True, "source": "page", "participants": participants, "title": "", "about": ""}


async def _http_get(url: str, params: dict | None) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, params=params)
        return response.status_code, response.text
