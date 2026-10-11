"""New customer projects from the public Kwork board. Read-only."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("leads.kwork")

BOARD_URL = "https://kwork.ru/projects"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    )
}
MOSCOW = timezone(timedelta(hours=3))


def _when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return parsed.astimezone(timezone.utc)


def _price(item: dict) -> str:
    raw = item.get("possiblePriceLimit")
    if isinstance(raw, bool) or raw is None:
        return ""
    try:
        amount = int(float(raw))
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    return f"{amount} ₽"


def _text(item: dict) -> str:
    parts = [item.get("name") or "", item.get("description") or "", _price(item)]
    return ". ".join(str(part).strip() for part in parts if str(part).strip())


def _orders(html: str) -> list[dict]:
    marker = '"wantsListData":'
    start = html.find(marker)
    if start < 0:
        return []
    try:
        payload, _end = json.JSONDecoder().raw_decode(html[start + len(marker) :])
    except json.JSONDecodeError:
        return []
    found: list[dict] = []
    now = datetime.now(timezone.utc)
    for item in (payload.get("pagination") or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("id") or "")
        if not project_id.isdigit():
            continue
        published = _when(str(item.get("date_active") or item.get("date_create") or ""))
        found.append(
            {
                "source": "kwork",
                "channel_username": "kwork",
                "channel_title": "Kwork",
                "channel_id": project_id,
                "message_id": int(project_id),
                "message_date": published.isoformat(timespec="seconds") if published else None,
                "text": _text(item),
                "url": f"https://kwork.ru/projects/{project_id}",
                "fresh": bool(published and published >= now - timedelta(days=1)),
            }
        )
    return found


async def fetch_orders() -> list[dict]:
    found: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
            for page in (1, 2):
                response = await client.get(BOARD_URL, params={"page": page})
                response.raise_for_status()
                found.extend(_orders(response.text))
    except httpx.HTTPError:
        log.warning("Kwork не отдал список заказов")
        return []
    unique: dict[str, dict] = {}
    for item in found:
        unique.setdefault(item["channel_id"], item)
    return list(unique.values())
