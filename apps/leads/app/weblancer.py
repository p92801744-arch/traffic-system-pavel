"""Заказы с Weblancer. Только чтение публичной ленты.

Сайт живой. С части датацентров Cloudflare отдаёт проверку вместо ленты:
тогда сборщик пишет предупреждение и идёт дальше. Разбор вёрстки тот же,
что у публичной страницы заказов.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import httpx

log = logging.getLogger("leads.weblancer")

BOARD_URL = "https://www.weblancer.net/freelance/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru",
}
MOSCOW = timezone(timedelta(hours=3))


def _plain(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = unescape(text).replace("<!-- -->", " ")
    return re.sub(r"\s+", " ", text).strip()


def _when(raw: str) -> datetime | None:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=MOSCOW).astimezone(timezone.utc)
    except ValueError:
        return None


def _blocked(html: str) -> bool:
    head = (html or "")[:4000].lower()
    return "just a moment" in head or "cf-mitigated" in head or "challenge-platform" in head


def _orders(html: str) -> list[dict]:
    if _blocked(html):
        return []
    found: list[dict] = []
    now = datetime.now(timezone.utc)
    for chunk in re.split(r'<h2 class="text-xl', html)[1:]:
        link = re.search(r'href="([^"]*?/freelance/[^"]*?-(\d+)/)"', chunk)
        title = re.search(r">([^<]+)</a>", chunk)
        if not link or not title:
            continue
        project_id = link.group(2)
        raw_href = link.group(1)
        if "weblancer.net" in raw_href:
            path = raw_href.split("weblancer.net", 1)[1]
        else:
            path = raw_href if raw_href.startswith("/") else f"/{raw_href}"
        desc = re.search(r'<p class="text-gray-600[^"]*"[^>]*>(.*?)</p>', chunk, re.S)
        price = re.search(r'class="text-green-600[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
        dated = re.search(r"<span>(\d{2}\.\d{2}\.\d{4})</span>", chunk)
        published = _when(dated.group(1) if dated else "")
        parts = [
            _plain(title.group(1)),
            _plain(desc.group(1) if desc else ""),
            _plain(price.group(1) if price else ""),
        ]
        found.append(
            {
                "source": "weblancer",
                "channel_username": "weblancer",
                "channel_title": "Weblancer",
                "channel_id": project_id,
                "message_id": int(project_id),
                "message_date": published.isoformat(timespec="seconds") if published else None,
                "text": ". ".join(part for part in parts if part),
                "url": "https://www.weblancer.net" + path,
                "fresh": bool(published and published >= now - timedelta(days=1)),
            }
        )
    return found


async def fetch_orders() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(BOARD_URL)
            if response.status_code in {401, 403, 429, 503} or _blocked(response.text):
                log.warning("Weblancer закрыл ленту проверкой, в этот заход заказов нет")
                return []
            response.raise_for_status()
    except httpx.HTTPError:
        log.warning("Weblancer не отдал список заказов")
        return []
    return _orders(response.text)
