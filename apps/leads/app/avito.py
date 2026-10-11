"""Vacancies from Avito Jobs. Uses a local Chrome profile; never opens service ads."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

log = logging.getLogger("leads.avito")

PORT = 9224
CHROME = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe"
PROFILE = Path(os.environ["LOCALAPPDATA"]) / "PavelLeads" / "avito-chrome"
# Bare "n8n" redirects into service ads — keep Jobs-only queries.
QUERIES = (
    "чат-бот",
    "wordpress",
    "python разработчик",
    "no-code",
)
BOARD_URL = "https://www.avito.ru/all/vakansii?s=104"

EXTRACT = r"""
(() => {
  if (!location.href.includes('/vakansii')) {
    return JSON.stringify({ ok: false, href: location.href, items: [] });
  }
  if (/доступ ограничен|проблема с IP/i.test(document.title + ' ' + (document.body && document.body.innerText || ''))) {
    return JSON.stringify({ ok: false, href: location.href, blocked: true, items: [] });
  }
  const items = [...document.querySelectorAll('[data-marker="item"]')].slice(0, 40).map(card => {
    const link = card.querySelector('a[itemprop="url"], a[data-marker="item-title"], a[href*="_"]');
    const title = (card.querySelector('[itemprop="name"], h3, a[data-marker="item-title"]') || {}).textContent || '';
    const price = (card.querySelector('[itemprop="price"], [data-marker="item-price"]') || {}).textContent || '';
    const href = link ? (link.getAttribute('href') || '') : '';
    const idMatch = href.match(/_(\d{8,11})(?:\?|$)/);
    const text = (card.innerText || '').trim().replace(/\s+/g, ' ');
    return {
      id: idMatch ? idMatch[1] : '',
      title: title.trim(),
      price: price.trim(),
      href: href.split('?')[0],
      text
    };
  }).filter(item => item.id && item.title && (item.href.includes('/vakansii/') || !item.href.includes('/predlozheniya_uslug/')));
  return JSON.stringify({ ok: true, href: location.href, items });
})()
"""


def _search_url(query: str) -> str:
    return f"https://www.avito.ru/all/vakansii?q={quote(query)}&s=104"


def _chrome_args() -> list[str]:
    PROFILE.mkdir(parents=True, exist_ok=True)
    return [
        str(CHROME),
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={PORT}",
        "--disable-quic",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        BOARD_URL,
    ]


async def _port_open() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"http://127.0.0.1:{PORT}/json/version")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


async def _ensure_chrome() -> None:
    if await _port_open():
        return
    if not CHROME.exists():
        log.warning("Авито Работа: Chrome не найден")
        return
    subprocess.Popen(_chrome_args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        await asyncio.sleep(1)
        if await _port_open():
            return
    log.warning("Авито Работа: Chrome с отладкой не поднялся")


async def _call(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg_id:
            return raw


async def _page_socket() -> str | None:
    async with httpx.AsyncClient(timeout=10) as client:
        tabs = (await client.get(f"http://127.0.0.1:{PORT}/json/list")).json()
        for tab in tabs:
            if tab.get("type") == "page" and "avito.ru" in (tab.get("url") or "") and tab.get("webSocketDebuggerUrl"):
                return tab["webSocketDebuggerUrl"]
        opened = (await client.put(f"http://127.0.0.1:{PORT}/json/new?{BOARD_URL}")).json()
        return opened.get("webSocketDebuggerUrl")


def _orders(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found: list[dict] = []
    for item in items:
        vacancy_id = str(item.get("id") or "")
        if not vacancy_id.isdigit():
            continue
        path = str(item.get("href") or "")
        if "predlozheniya_uslug" in path:
            continue
        if path and not path.startswith("http"):
            path = "https://www.avito.ru" + path
        text = ". ".join(
            part
            for part in (
                item.get("title") or "",
                item.get("price") or "",
                item.get("text") or "",
            )
            if str(part).strip()
        )
        found.append(
            {
                "source": "avito",
                "channel_username": "avito",
                "channel_title": "Авито Работа",
                "channel_id": vacancy_id,
                "message_id": int(vacancy_id),
                "message_date": now,
                "text": text,
                "url": path or f"https://www.avito.ru/all/vakansii?q={vacancy_id}",
                "fresh": True,
            }
        )
    return found


async def _read_page(ws, msg_id: int) -> dict:
    result = await _call(
        ws,
        msg_id,
        "Runtime.evaluate",
        {"expression": EXTRACT, "returnByValue": True},
    )
    raw = (result.get("result") or {}).get("result", {}).get("value") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def _fetch_query(ws, query: str, msg_id: int) -> list[dict]:
    await _call(ws, msg_id, "Page.navigate", {"url": _search_url(query)})
    payload: dict = {}
    for attempt in range(8):
        await asyncio.sleep(2 if attempt else 4)
        payload = await _read_page(ws, msg_id + 1 + attempt)
        if payload.get("blocked"):
            log.warning("Авито Работа временно ограничил доступ")
            return []
        if payload.get("ok") and payload.get("items"):
            return _orders(payload.get("items") or [])
        if payload.get("ok") is False and "predlozheniya_uslug" in str(payload.get("href") or ""):
            log.info("Авито увёл запрос «%s» в услуги", query)
            return []
    if not payload.get("ok"):
        log.info("Авито увёл запрос «%s» из вакансий", query)
        return []
    return _orders(payload.get("items") or [])


async def fetch_orders() -> list[dict]:
    await _ensure_chrome()
    if not await _port_open():
        return []
    socket = await _page_socket()
    if not socket:
        log.warning("Авито Работа: нет вкладки")
        return []
    collected: dict[str, dict] = {}
    try:
        async with websockets.connect(socket, max_size=20_000_000) as ws:
            for index, query in enumerate(QUERIES, start=10):
                try:
                    items = await _fetch_query(ws, query, index * 10)
                except Exception:
                    log.warning("Авито Работа не отдал выдачу по запросу %s", query)
                    continue
                if not items and query == QUERIES[0]:
                    # First empty page after a block - do not hammer the rest.
                    sample = await _read_page(ws, 900)
                    if sample.get("blocked"):
                        break
                for item in items:
                    collected.setdefault(item["channel_id"], item)
                await asyncio.sleep(5)
    except Exception:
        log.warning("Авито Работа: браузерная сессия не ответила")
        return []
    return list(collected.values())
