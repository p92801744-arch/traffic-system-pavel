"""Заказы с Freelance.ru. Это отдельная биржа, не FL.ru. Только чтение."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import httpx

log = logging.getLogger("leads.freelance_ru")

BOARD_URL = "https://freelance.ru/projects/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru",
}
MOSCOW = timezone(timedelta(hours=3))


def _plain(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
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


def _orders(html: str) -> list[dict]:
    found: list[dict] = []
    now = datetime.now(timezone.utc)
    for chunk in re.findall(r'<article class="task-card[^"]*">(.*?)</article>', html, re.S):
        link = re.search(r'href="(?:https://freelance\.ru)?(/task/view/(\d+))"', chunk)
        title = re.search(r'class="task-card__title-link"[^>]*>(.*?)</a>', chunk, re.S)
        if not link or not title:
            continue
        project_id = link.group(2)
        desc = re.search(r'class="task-card__desc"[^>]*>(.*?)</p>', chunk, re.S)
        budget = re.search(r'class="task-card__budget"[^>]*>(.*?)</div>', chunk, re.S)
        dated = re.search(r'title="(\d{2}\.\d{2}\.\d{4}[^"]*)"', chunk)
        published = _when(dated.group(1) if dated else "")
        parts = [
            _plain(title.group(1)),
            _plain(desc.group(1) if desc else ""),
            _plain(budget.group(1) if budget else ""),
        ]
        found.append(
            {
                "source": "freelance_ru",
                "channel_username": "freelance_ru",
                "channel_title": "Freelance.ru",
                "channel_id": project_id,
                "message_id": int(project_id),
                "message_date": published.isoformat(timespec="seconds") if published else None,
                "text": ". ".join(part for part in parts if part),
                "url": f"https://freelance.ru/task/view/{project_id}",
                "fresh": bool(published and published >= now - timedelta(days=1)),
            }
        )
    return found


async def fetch_orders() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(BOARD_URL)
            response.raise_for_status()
    except httpx.HTTPError:
        log.warning("Freelance.ru не отдал список заказов")
        return []
    return _orders(response.text)
