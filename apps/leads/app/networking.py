"""Встречи в Петербурге. Отдельно от заявок: дата, место, ссылка, тема, зачем идти."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from html import unescape

import httpx

from app import db

log = logging.getLogger("leads.networking")

PAUSE_SECONDS = 3 * 60 * 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru",
}
MEROP_URL = "https://xn--e1aganlgfct3kc.xn--p1acf/city/sankt-peterburg"
TIMEPAD_URL = "https://api.timepad.ru/v1/events.json"
# @digital_spb в открытом превью показывает анонсы 2022 года, поэтому его нет.
TG_CHANNELS = (("bizspbnews", False, "Афиша бизнес-мероприятий Петербурга"),)

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

RELEVANT = re.compile(
    r"(бизнес[-\s]?завтрак|(?<![а-я])бизнес(?![а-я])|нетворкинг|предпринимател|"
    r"основател|стартап|митап|meetup|конференц|форум|маркетинг|e-?com|"
    r"интернет-магазин|маркетплейс|селлер|wildberries|ozon|вайлдберриз|"
    r"(?<![а-я])it(?![а-я])|айти|разработк|автоматизац|продаж|(?<![а-я])лид)",
    re.IGNORECASE,
)

_RU_DATE = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*[–—-]\s*\d{1,2})?\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"(?:\s+(20\d{2}))?",
    re.IGNORECASE,
)
_NUM_DATE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(20\d{2})(?!\d)")


def _plain(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def title_key(title: str) -> str:
    lowered = (title or "").lower().replace("ё", "е")
    lowered = re.sub(r"[^a-zа-я0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()[:80]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def event_start(text: str, today: date) -> tuple[date | None, str]:
    """Первая ещё не прошедшая дата в тексте. Если все в прошлом — самая ранняя."""
    found: list[tuple[date, str]] = []
    for match in _RU_DATE.finditer(text or ""):
        day = int(match.group(1))
        month = MONTHS[match.group(2).lower()]
        year = int(match.group(3)) if match.group(3) else today.year
        parsed = _safe_date(year, month, day)
        if parsed is None:
            continue
        if not match.group(3) and parsed < today - timedelta(days=21) and today.month >= 11 and month <= 2:
            parsed = _safe_date(year + 1, month, day) or parsed
        found.append((parsed, re.sub(r"\s+", " ", match.group(0)).strip()))
    for match in _NUM_DATE.finditer(text or ""):
        parsed = _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if parsed:
            found.append((parsed, match.group(0)))
    if not found:
        return None, ""
    upcoming = [item for item in found if item[0] >= today]
    return (upcoming or found)[0]


def why_for(text: str, default: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    if any(word in low for word in ("маркетплейс", "wildberries", "ozon", "селлер", "вайлдберриз")):
        return "Встреча продавцов и маркетплейсов: рядом люди, которым нужны карточки, каталог и кабинет."
    if "маркет" in low:
        return "Маркетинговая встреча: можно говорить про трафик, контент и систему публикаций."
    if any(word in low for word in ("1с", "битрикс", "мойсклад", "мой склад")):
        return "Встреча про учёт и CRM: рядом те, кому нужна связка сайта, склада и 1С."
    if any(word in low for word in ("завтрак", "предпринимат", "нетворк", "основател", "стартап")):
        return "Предпринимательская встреча в Петербурге: новые люди, не вакансия."
    if any(word in low for word in ("митап", "meetup", "конференц", "айти", " it")):
        return "IT-встреча в Петербурге: можно познакомиться с теми, кто делает продукты и магазины."
    return default


def _event(
    *,
    source: str,
    external_id: str,
    title: str,
    starts_on: date,
    when_label: str,
    place: str,
    url: str,
    topic: str,
    why: str,
) -> dict:
    return {
        "source": source,
        "external_id": external_id,
        "title": title.strip(),
        "starts_on": starts_on.isoformat(),
        "when_label": when_label or starts_on.strftime("%d.%m.%Y"),
        "place": place.strip() or "Санкт-Петербург",
        "url": url,
        "topic": topic.strip() or title.strip(),
        "why": why.strip(),
        "title_key": title_key(title),
    }


def parse_timepad(payload: dict, today: date) -> list[dict]:
    found: list[dict] = []
    for item in payload.get("values") or []:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        city = str(location.get("city") or "")
        if "петербург" not in city.lower() and "спб" not in city.lower():
            continue
        name = str(item.get("name") or "").strip()
        about = str(item.get("description_short") or "").strip()
        blob = f"{name}. {about}"
        if not RELEVANT.search(blob):
            continue
        starts, label = event_start(str(item.get("starts_at") or ""), today)
        if starts is None or starts < today:
            raw = str(item.get("starts_at") or "")
            try:
                starts = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                continue
            label = starts.strftime("%d.%m.%Y")
        if starts < today:
            continue
        address = str(location.get("address") or "").strip()
        place = ", ".join(part for part in (city, address) if part) or "Санкт-Петербург"
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        found.append(
            _event(
                source="timepad",
                external_id=str(item.get("id") or url),
                title=name,
                starts_on=starts,
                when_label=label,
                place=place,
                url=url,
                topic=about or name,
                why=why_for(blob, "Деловая или техническая встреча в Петербурге."),
            )
        )
    return found


def parse_merop(html: str, today: date) -> list[dict]:
    found: list[dict] = []
    for klass, chunk in re.findall(r'<article class="(event-card[^"]*)">(.*?)</article>', html or "", re.S):
        if "is-muted" in klass:
            continue
        link = re.search(r'href="(https?://[^"]+)"', chunk)
        title = re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.S)
        if not link or not title:
            continue
        name = _plain(title.group(1))
        spans = [_plain(piece) for piece in re.findall(r"<span>(.*?)</span>", chunk, re.S)]
        spans = [piece for piece in spans if piece]
        when_raw = next((piece for piece in spans if _RU_DATE.search(piece) or _NUM_DATE.search(piece)), "")
        starts, label = event_start(when_raw or name, today)
        if starts is None or starts < today:
            continue
        city = next((piece for piece in spans if "петербург" in piece.lower() or piece.lower() == "спб"), "")
        kind = next((piece for piece in spans if piece not in {when_raw, city}), "")
        about = _plain(re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S).group(1)) if re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S) else ""
        place = city or "Санкт-Петербург"
        if kind and "онлайн" in kind.lower() and "офлайн" not in kind.lower():
            place = f"{place}, онлайн"
        found.append(
            _event(
                source="merop",
                external_id=link.group(1),
                title=name,
                starts_on=starts,
                when_label=label or when_raw,
                place=place,
                url=link.group(1),
                topic=about or kind or name,
                why=why_for(
                    f"{name}. {about}",
                    "IT-конференция или митап в Петербурге: рядом люди, которые делают продукты и магазины.",
                ),
            )
        )
    return found


def _event_title(raw: str, plain: str) -> str:
    """Первый жирный фрагмент с буквами. В превью Telegram первый <b> часто эмодзи."""
    for piece in re.findall(r"<b>(.*?)</b>", raw or "", re.S):
        title = _plain(piece).strip(" -–—")
        letters = re.sub(r"[^0-9a-zа-я]", "", title.lower())
        if len(letters) >= 8:
            return title[:140]
    return plain.split("\n", 1)[0].strip(" -–—")[:140]


def parse_telegram(html: str, username: str, today: date, *, assume_relevant: bool) -> list[dict]:
    found: list[dict] = []
    chunks = re.split(r'data-post="' + re.escape(username) + r'/(\d+)"', html or "")
    # split keeps the id as its own piece: [pre, id, body, id, body, ...]
    for index in range(1, len(chunks), 2):
        message_id = chunks[index]
        body = chunks[index + 1] if index + 1 < len(chunks) else ""
        text_match = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>\s*<div class="tgme_widget_message_footer', body, re.S)
        if not text_match:
            text_match = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', body, re.S)
        if not text_match:
            continue
        raw = text_match.group(1)
        plain = _plain(raw)
        if len(plain) < 40:
            continue
        if not assume_relevant and not RELEVANT.search(plain):
            continue
        starts, label = event_start(plain, today)
        if starts is None or starts < today:
            continue
        title = _event_title(raw, plain)
        if len(title) < 8:
            continue
        place = "Санкт-Петербург"
        if re.search(r"\bонлайн\b", plain, re.I) and not re.search(r"офлайн|адрес|метро", plain, re.I):
            place = "Санкт-Петербург, онлайн"
        found.append(
            _event(
                source=f"tg:{username}",
                external_id=message_id,
                title=title,
                starts_on=starts,
                when_label=label,
                place=place,
                url=f"https://t.me/{username}/{message_id}",
                topic=plain[:280],
                why=why_for(plain, "Анонс встречи в Петербурге из открытого канала."),
            )
        )
    return found


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response | None:
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError:
        log.warning("Источник встреч не ответил: %s", url)
        return None
    if response.status_code >= 400:
        log.warning("Источник встреч ответил %s: %s", response.status_code, url)
        return None
    return response


async def collect(today: date | None = None) -> list[dict]:
    today = today or date.today()
    found: list[dict] = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
        timepad = await _get(
            client,
            TIMEPAD_URL,
            {
                "limit": 40,
                "cities": "Санкт-Петербург",
                "fields": "location,description_short",
                "sort": "+starts_at",
                "starts_at_min": today.isoformat(),
            },
        )
        if timepad is not None:
            try:
                found.extend(parse_timepad(timepad.json(), today))
            except ValueError:
                log.warning("Timepad отдал не JSON")
        merop = await _get(client, MEROP_URL)
        if merop is not None:
            found.extend(parse_merop(merop.text, today))
        for username, assume, _title in TG_CHANNELS:
            page = await _get(client, f"https://t.me/s/{username}")
            if page is None:
                continue
            found.extend(parse_telegram(page.text, username, today, assume_relevant=assume))
    return found


async def refresh(today: date | None = None) -> int:
    today = today or date.today()
    items = await collect(today)
    conn = await db.connect()
    try:
        saved = 0
        for item in items:
            if item["starts_on"] < today.isoformat():
                continue
            if await db.upsert_event(conn, item):
                saved += 1
        await db.delete_past_events(conn, today.isoformat())
        log.info("Нетворкинг: новых встреч %s", saved)
        return saved
    finally:
        await conn.close()


async def upcoming() -> list[dict]:
    conn = await db.connect()
    try:
        await db.delete_past_events(conn, date.today().isoformat())
        return await db.list_events(conn, date.today().isoformat())
    finally:
        await conn.close()


async def poll_forever() -> None:
    while True:
        try:
            await refresh()
        except Exception:
            log.exception("Сбор встреч оборвался")
        await asyncio.sleep(PAUSE_SECONDS)
