"""Vacancies from Avito Jobs. Uses a local Chrome profile; never opens service ads."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

log = logging.getLogger("leads.avito")

PORT = 9224
CHROME = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe"
PROFILE = Path(os.environ["LOCALAPPDATA"]) / "PavelLeads" / "avito-chrome"
# Запросы проверены вручную 2026-10-12 в категории Работа/Вакансии (вся Россия).
# Не брать: n8n, интеграция crm, amocrm, битрикс24, телеграм бот, разработчик сайтов, парсинг,
# rpa, специалист по нейросетям, chatgpt, backend разработчик (уводит в услуги или общий поиск);
# чат-бот, менеджер маркетплейсов, интернет-магазин, интегратор, технический специалист,
# администратор сайта, администратор crm, внедрение crm, специалист amocrm/битрикс24,
# специалист по автоматизации, it-специалист, ведение соцсетей (курьеры, кафе, продажи, охрана);
# no-code/low-code разработчик, промпт-инженер (пусто); сфера «IT» с запросом уводит из вакансий.
# Сфера «Маркетинг, реклама и PR» с запросом держит выдачу чистой: там SMM и маркетологи.
MARKETING = "marketing_reklama_i_pr-ASgBAgICAUSOC_qXkAM"
QUERIES: tuple[tuple[str, str], ...] = (
    ("", "автоматизация бизнес процессов"),
    ("", "make автоматизация"),
    ("", "инженер по автоматизации бизнес-процессов"),
    ("", "специалист по внедрению ии"),
    ("", "ai-специалист"),
    ("", "ai-разработчик"),
    ("", "специалист n8n"),
    ("", "разработчик ботов"),
    ("", "разработчик чат-ботов"),
    ("", "разработчик телеграм ботов"),
    ("", "python-разработчик"),
    ("", "fullstack"),
    ("", "frontend"),
    ("", "веб-разработчик"),
    ("", "программист"),
    ("", "wordpress"),
    ("", "контент-администратор сайта"),
    ("", "технический администратор сайта"),
    ("", "контент-менеджер маркетплейсов"),
    ("", "карточки товаров"),
    ("", "seo-специалист"),
    ("", "seo копирайтер"),
    ("", "копирайтер"),
    ("", "контент-менеджер"),
    ("", "таргетолог"),
    (MARKETING, "smm"),
    (MARKETING, "маркетолог"),
    (MARKETING, "интернет-маркетолог"),
)
# Фильтр по названию вакансии (только Авито): в заголовке должно быть слово из профиля.
TITLE_OK = re.compile(
    r"(разраб|программист|автоматиз|\bsmm\b|смм|контент|копирайт|таргет|маркетолог|\bseo\b|сео|"
    r"сайт|\bcrm\b|срм|битрикс|bitrix|amo|1с|1c|(?<![а-яё])ии(?![а-яё])|\bai\b|ai-|нейросет|"
    r"бот|python|fullstack|backend|frontend|front-end|веб|\bweb|n8n|внедрен|digital|wordpress|"
    r"tilda|тильд|интернет-магазин|интернет магазин|карточ|e-commerce|ecommerce|системн\w* администратор|"
    r"it-|\bit\b|айти|аналитик|интегратор|no-code|low-code|продвижен|соцсет|reels|трафик)",
    re.IGNORECASE,
)
# Явный шум, даже если слово профиля случайно попало в заголовок.
TITLE_NOISE = re.compile(
    r"(?<![а-яё])(аптек|асу тп|кипиа|охран|курьер|водител|грузчик|кассир|продавец|риелтор|риэлтор|"
    r"менеджер по продажам|менеджер по привлечению|менеджер по работе с клиент|отдел продаж|"
    r"оператор call|колл-центр|call-центр|платное обучение|обучение платно|с нуля, обучение|тестирован|qa|переводчик)",
    re.IGNORECASE,
)


def title_relevant(title: str) -> bool:
    title = title or ""
    return bool(TITLE_OK.search(title)) and not TITLE_NOISE.search(title)


# Регион в пути URL и подпись в ленте.
REGIONS = (("all", "Авито Работа"), ("sankt-peterburg", "Авито Работа · СПб"))
PER_REGION = 7          # запросов на регион за заход, по кругу (28 запросов = 4 захода)
PAUSE = (8.0, 14.0)     # пауза между запросами, секунд
MIN_INTERVAL = timedelta(minutes=55)
BLOCK_COOLDOWN = timedelta(hours=3)
STATE_PATH = Path(os.environ["LOCALAPPDATA"]) / "PavelLeads" / "avito-state.json"
VACANCY_PATH = re.compile(r"^https://www\.avito\.ru/(all|sankt-peterburg)/vakansii(?:[/?#]|$)")
BOARD_URL = "https://www.avito.ru/all/vakansii?s=104"

EXTRACT = r"""
(() => {
  if (!/^https:\/\/www\.avito\.ru\/[a-z_-]+\/vakansii(?:[\/?#]|$)/.test(location.href)) {
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
  }).filter(item => item.id && item.title && item.href.includes('/vakansii/'));
  return JSON.stringify({ ok: true, href: location.href, items });
})()
"""


def _search_url(query: str, region: str = "all", section: str = "") -> str:
    path = f"vakansii/{section}" if section else "vakansii"
    return f"https://www.avito.ru/{region}/{path}?q={quote(query)}&s=104"


def in_vacancies(href: str) -> bool:
    """Страница осталась в Работа/Вакансии, а не ушла в услуги или общий поиск."""
    return bool(VACANCY_PATH.match(href or ""))


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def plan_run(state: dict, now: datetime) -> list[tuple[str, str, str, str]] | None:
    """Какие (регион, подпись, запрос) идут в этот заход. None — рано или после блокировки."""
    for key, gap in (("last_run", MIN_INTERVAL), ("blocked_at", BLOCK_COOLDOWN)):
        raw = state.get(key)
        if raw:
            try:
                if now - datetime.fromisoformat(raw) < gap:
                    return None
            except ValueError:
                pass
    offset = int(state.get("offset") or 0) % len(QUERIES)
    batch = [QUERIES[(offset + index) % len(QUERIES)] for index in range(PER_REGION)]
    return [(region, title, section, query) for region, title in REGIONS for section, query in batch]


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


def _orders(items: list[dict], channel_title: str = "Авито Работа") -> list[dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found: list[dict] = []
    for item in items:
        vacancy_id = str(item.get("id") or "")
        if not vacancy_id.isdigit():
            continue
        path = str(item.get("href") or "")
        if "predlozheniya_uslug" in path or (path and "/vakansii/" not in path):
            continue
        if not title_relevant(str(item.get("title") or "")):
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
                "channel_title": channel_title,
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


async def _fetch_query(
    ws, query: str, msg_id: int, region: str = "all", title: str = "Авито Работа", section: str = ""
) -> list[dict] | None:
    """Выдача по запросу. None — Авито ограничил доступ."""
    await _call(ws, msg_id, "Page.navigate", {"url": _search_url(query, region, section)})
    payload: dict = {}
    for attempt in range(8):
        await asyncio.sleep(2 if attempt else 4)
        payload = await _read_page(ws, msg_id + 1 + attempt)
        if payload.get("blocked"):
            log.warning("Авито Работа временно ограничил доступ")
            return None
        href = str(payload.get("href") or "")
        if href and not in_vacancies(href) and "/vakansii" not in href:
            log.info("Авито увёл запрос «%s» (%s) из вакансий, пропускаю", query, region)
            return []
        if payload.get("ok") and payload.get("items"):
            return _orders(payload.get("items") or [], title)
    if not payload.get("ok") or not in_vacancies(str(payload.get("href") or "")):
        log.info("Авито увёл запрос «%s» (%s) из вакансий", query, region)
        return []
    return _orders(payload.get("items") or [], title)


async def browser_html(url: str, wait: float = 4, tries: int = 5) -> str:
    """Открыть публичную страницу в той же локальной Chrome-вкладке и вернуть HTML.

    Нужен для лент, которые простому HTTP-клиенту отдают проверку Cloudflare.
    Ничего не нажимает и не входит в аккаунты.
    """
    await _ensure_chrome()
    if not await _port_open():
        return ""
    socket = await _page_socket()
    if not socket:
        return ""
    html = ""
    async with websockets.connect(socket, max_size=40_000_000) as ws:
        await _call(ws, 5000, "Page.navigate", {"url": url})
        for attempt in range(tries):
            await asyncio.sleep(wait)
            result = await _call(
                ws,
                5001 + attempt,
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            )
            html = (result.get("result") or {}).get("result", {}).get("value") or ""
            if html and "just a moment" not in html[:4000].lower():
                break
        # Вернуть вкладку на Авито, иначе следующий заход откроет новую вкладку.
        await _call(ws, 5100, "Page.navigate", {"url": BOARD_URL})
    return html


async def browser_cookies(url: str) -> list[dict]:
    """Куки сайта из локального Chrome сборщика (вход делает сам Павел). Только чтение."""
    if not await _port_open():
        return []
    socket = await _page_socket()
    if not socket:
        return []
    async with websockets.connect(socket, max_size=20_000_000) as ws:
        result = await _call(ws, 5200, "Network.getCookies", {"urls": [url]})
    cookies = (result.get("result") or {}).get("cookies") or []
    return [
        {"host": item.get("domain") or "", "name": item.get("name") or "", "value": item.get("value") or ""}
        for item in cookies
        if item.get("name") and item.get("value")
    ]


async def fetch_orders(force: bool = False) -> list[dict]:
    state = _load_state()
    now = datetime.now(timezone.utc)
    plan = plan_run({} if force else state, now)
    if plan is None:
        return []
    await _ensure_chrome()
    if not await _port_open():
        return []
    socket = await _page_socket()
    if not socket:
        log.warning("Авито Работа: нет вкладки")
        return []
    collected: dict[str, dict] = {}
    stats: list[str] = []
    try:
        async with websockets.connect(socket, max_size=40_000_000) as ws:
            for index, (region, title, section, query) in enumerate(plan, start=10):
                try:
                    items = await _fetch_query(ws, query, index * 10, region, title, section)
                except Exception:
                    log.warning("Авито Работа не отдал выдачу по запросу %s", query)
                    items = []
                if items is None:
                    state["blocked_at"] = now.isoformat(timespec="seconds")
                    break
                stats.append(f"{region}:{query}={len(items)}")
                for item in items:
                    collected.setdefault(item["channel_id"], item)
                await asyncio.sleep(random.uniform(*PAUSE))
            await _call(ws, 9000, "Page.navigate", {"url": BOARD_URL})
    except Exception:
        log.warning("Авито Работа: браузерная сессия не ответила")
    state["last_run"] = now.isoformat(timespec="seconds")
    state["offset"] = (int(state.get("offset") or 0) + PER_REGION) % len(QUERIES)
    _save_state(state)
    log.info("Авито Работа: %s; всего %s", ", ".join(stats), len(collected))
    return list(collected.values())
