"""Orders from the specialist's Profi.ru board. Read-only: never sends a reply."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.settings import ROOT

log = logging.getLogger("leads.profi")

SESSION_PATH = ROOT / "data" / "profi-session.json"
QUERY_PATH = Path(__file__).with_name("profi_board.json")
BOARD_URL = "https://profi.ru/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://profi.ru",
    "Referer": "https://profi.ru/backoffice/n.php",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "x-app-id": "BO",
    "x-warp-ui-app": "WEBBO",
    "x-warp-ui-type": "WEB",
    "x-warp-ui-ver": "1.0",
    "x-new-auth-compatible": "1",
}


def _cookies() -> dict[str, str]:
    if not SESSION_PATH.exists():
        return {}
    try:
        rows = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    jar: dict[str, str] = {}
    for row in rows:
        host = str(row.get("host") or "")
        name = str(row.get("name") or "")
        value = str(row.get("value") or "")
        if "profi.ru" in host and name and value:
            jar[name] = value
    return jar


def _money(price: dict | None) -> str:
    if not price:
        return ""
    parts = [str(price.get(key) or "").strip() for key in ("prefix", "value", "suffix")]
    return " ".join(part for part in parts if part)


def _place(item: dict) -> str:
    geo = item.get("geo") or {}
    remote = geo.get("remote") or {}
    if remote:
        return " ".join(part for part in (remote.get("prefix"), remote.get("suffix")) if part)
    location = geo.get("orderLocation") or {}
    places = location.get("geoplaces") or []
    name = ""
    if places and isinstance(places[0], dict):
        name = places[0].get("name") or ""
    return " ".join(part for part in (location.get("prefix"), name, location.get("suffix")) if part)


def _when(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and not value.isdigit():
        return value
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp > 10_000_000_000:
        stamp /= 1000
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _text(item: dict) -> str:
    client = (item.get("clientInfo") or {}).get("name") or ""
    parts = [item.get("title") or "", item.get("description") or "", _money(item.get("price")), _place(item), client]
    return ". ".join(str(part).strip() for part in parts if str(part).strip())


def _orders(payload: dict) -> list[dict]:
    board = ((payload.get("data") or {}).get("boSearchBoardItems") or {})
    found: list[dict] = []
    for item in board.get("items") or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        order_id = str(item.get("id") or "")
        if not order_id.isdigit():
            continue
        found.append(
            {
                "source": "profi",
                "channel_username": "profi",
                "channel_title": "Профи.ру",
                "channel_id": order_id,
                "message_id": int(order_id),
                "message_date": _when(item.get("lastUpdateDate")),
                "text": _text(item),
                "url": f"https://profi.ru/backoffice/n.php?o={order_id}",
                "fresh": bool(item.get("isFresh")),
            }
        )
    return found


async def fetch_orders() -> list[dict]:
    cookies = _cookies()
    if not cookies:
        log.info("Профи.ру: входа нет, ленту пропускаю")
        return []
    spec = json.loads(QUERY_PATH.read_text(encoding="utf-8"))
    try:
        async with httpx.AsyncClient(timeout=25, cookies=cookies, headers=HEADERS) as client:
            response = await client.post(BOARD_URL, json=spec)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        log.warning("Профи.ру не отдал ленту")
        return []
    if payload.get("errors"):
        log.warning("Профи.ру не пустил к заказам")
        return []
    return _orders(payload)
