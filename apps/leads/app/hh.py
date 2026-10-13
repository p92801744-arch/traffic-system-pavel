"""Вакансии HeadHunter: только удалёнка или проектная работа. Только чтение.

Публичный API (api.hh.ru/vacancies) с 2026 года без токена отвечает 403 forbidden
(проверено 2026-10-13), поэтому берём RSS поиска hh.ru: он принимает те же фильтры
(work_format, employment, search_field, order_by). В RSS нет описания вакансии, только
название, компания, регион и доход, поэтому решает название (фильтр как у Авито).
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.avito import title_relevant

log = logging.getLogger("leads.hh")

RSS_URL = "https://hh.ru/search/vacancy/rss"
HEADERS = {
    "User-Agent": "PavelLeads/1.0 (pasha16092003@yandex.ru)",
    "HH-User-Agent": "PavelLeads/1.0 (pasha16092003@yandex.ru)",
}
# Поиск по названию вакансии. Проверено вручную 2026-10-13.
# Не брать: frontend, карточки товаров (после фильтра 0 заявок).
QUERIES = (
    "n8n",
    "автоматизац",
    "ai",
    "ии",
    "нейросет",
    "чат-бот",
    "python",
    "fullstack",
    "веб-разработчик",
    "wordpress",
    "контент-менеджер",
    "администратор сайта",
    "seo",
    "копирайтер",
    "smm",
    "таргетолог",
    "маркетолог",
)
# Удалёнка (любая занятость, штат тоже) и проектная работа (любой формат).
MODES = (
    ({"work_format": "REMOTE"}, "Удалённо"),
    ({"employment": "project"}, "Проектная работа"),
)
FRESH_DAYS = 3
PAUSE = 1.5
MIN_INTERVAL = timedelta(minutes=55)
_last_run: datetime | None = None


def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if "T" in raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _description(raw: str) -> str:
    """«Вакансия компании: X Создана: … Регион: … доход: …» → «Компания X. Регион … Доход …»."""
    text = _clean(raw)
    text = re.sub(r"Вакансия компании:\s*", "Компания: ", text)
    text = re.sub(r"\s*Создана:\s*\S+", "", text)
    text = re.sub(r"Предполагаемый уровень месячного дохода:", "Доход:", text)
    return text.strip()


def items_from_rss(xml_text: str, mode_label: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    found: list[dict] = []
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        match = re.search(r"/vacancy/(\d+)", link)
        if not match:
            continue
        title = (item.findtext("title") or "").strip()
        published = _date(item.findtext("pubDate") or "")
        if published and published < now - timedelta(days=FRESH_DAYS):
            continue
        if not title_relevant(title):
            continue
        details = _description(item.findtext("description") or "")
        text = ". ".join(part for part in (title, details, mode_label) if part)
        found.append(
            {
                "source": "hh",
                "channel_username": "hh",
                "channel_title": "HeadHunter",
                "channel_id": match.group(1),
                "message_id": int(match.group(1)),
                "message_date": published.isoformat(timespec="seconds") if published else None,
                "text": text,
                "url": f"https://hh.ru/vacancy/{match.group(1)}",
                "fresh": bool(published and published >= now - timedelta(days=1)),
            }
        )
    return found


async def fetch_orders(force: bool = False) -> list[dict]:
    global _last_run
    now = datetime.now(timezone.utc)
    if not force and _last_run and now - _last_run < MIN_INTERVAL:
        return []
    _last_run = now
    collected: dict[str, dict] = {}
    stats: list[str] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        for query in QUERIES:
            for params, label in MODES:
                try:
                    response = await client.get(
                        RSS_URL,
                        params={"text": query, "area": "113", "search_field": "name",
                                "order_by": "publication_time", **params},
                    )
                    response.raise_for_status()
                    items = items_from_rss(response.text, label, now)
                except Exception:
                    log.warning("HeadHunter не отдал ленту по запросу %s (%s)", query, label)
                    items = []
                stats.append(f"{query}/{label[:4]}={len(items)}")
                for item in items:
                    collected.setdefault(item["channel_id"], item)
                await asyncio.sleep(PAUSE)
    log.info("HeadHunter: %s; всего %s", ", ".join(stats), len(collected))
    return list(collected.values())
