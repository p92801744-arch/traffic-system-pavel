from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from app import db
from app.filters import intake_hits, is_offer_card, load_sources
from app.avito import fetch_orders as fetch_avito
from app.freelance_ru import fetch_orders as fetch_freelance_ru
from app.hh import fetch_orders as fetch_hh
from app.kwork import fetch_orders as fetch_kwork
from app.profi import fetch_orders
from app.weblancer import fetch_orders as fetch_weblancer
from app.scorer import score_lead

log = logging.getLogger("leads.boards")

PAUSE_SECONDS = 20 * 60
HEADERS = {"User-Agent": "PavelLeadsInbox/1.0"}


def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _hh_items(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    found: list[dict] = []
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        match = re.search(r"/vacancy/(\d+)", link)
        if not match:
            continue
        title = (item.findtext("title") or "").strip()
        description = _clean(item.findtext("description") or "")
        published = _parse_date(item.findtext("pubDate") or "")
        found.append(
            {
                "source": "hh",
                "channel_username": "hh",
                "channel_title": "HeadHunter",
                "channel_id": match.group(1),
                "message_id": int(match.group(1)),
                "message_date": published.isoformat(timespec="seconds") if published else None,
                "text": f"{title}. {description}".strip(),
                "url": link,
                "fresh": bool(published and published >= datetime.now(timezone.utc) - timedelta(days=1)),
            }
        )
    return found


def _parse_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def fl_maintenance(html: str) -> bool:
    return "технические работы" in (html or "")[:20000].lower()


def _fl_items(html: str) -> list[dict]:
    found: list[dict] = []
    pairs = re.findall(
        r'"url":"(https:\\/\\/www\.fl\.ru\\/projects\\/(\d+)\\/[^"]*)","name":"((?:\\.|[^"\\])*)"',
        html,
    )
    for raw_url, project_id, raw_name in pairs:
        link = json.loads(f'"{raw_url}"')
        name = json.loads(f'"{raw_name}"')
        found.append(
            {
                "source": "fl",
                "channel_username": "fl",
                "channel_title": "FL.ru",
                "channel_id": project_id,
                "message_id": int(project_id),
                "message_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "text": name,
                "url": link,
                "fresh": True,
            }
        )
    return found


async def _save(item: dict, sources: dict) -> bool:
    if len(item["text"]) < 40:
        return False
    # Kwork, Авито и другие доски отдают и заказы, и витрины услуг.
    if is_offer_card(item["text"]):
        return False
    hits = intake_hits(item["text"], sources, assume_request=True)
    if not hits:
        return False
    conn = await db.connect()
    try:
        lead_id = await db.upsert_lead(
            conn,
            {
                "source": item["source"],
                "channel_username": item["channel_username"],
                "channel_title": item["channel_title"],
                "channel_id": item["channel_id"],
                "message_id": item["message_id"],
                "message_date": item["message_date"],
                "text": item["text"],
                "url": item["url"],
                "keywords": hits,
            },
        )
        if not lead_id:
            return False
        score = await score_lead(item["text"], item["channel_username"])
        await db.save_score(conn, lead_id, score)
        if item["source"] == "kwork":
            from app.reply import auto_reply

            try:
                await auto_reply(lead_id)
            except Exception:
                log.exception("Отклик Kwork для %s не собрался", lead_id)
        if item["fresh"]:
            from app.notifier import send_lead

            lead = await db.get_lead(conn, lead_id)
            if lead:
                await send_lead(lead)
        return True
    finally:
        await conn.close()


async def poll_once() -> int:
    sources = load_sources()
    saved = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        try:
            hh_items = await fetch_hh()
        except Exception:
            log.warning("HeadHunter: сборщик упал")
            hh_items = []
        for item in hh_items:
            if await _save(item, sources):
                saved += 1
        try:
            response = await client.get("https://www.fl.ru/projects/", params={"kind": "5"})
            response.raise_for_status()
            if fl_maintenance(response.text):
                log.warning("FL.ru на технических работах, заказов нет")
            fl_items = _fl_items(response.text)
            if not fl_items and not fl_maintenance(response.text):
                log.warning("FL.ru: страница пришла, но заказов не разобрано, вёрстка могла смениться")
        except Exception:
            log.warning("FL.ru не отдал список заказов")
            fl_items = []
        for item in fl_items[:30]:
            if await _save(item, sources):
                saved += 1
        # Хабр Фриланс (freelance.habr.com) закрыт 28 февраля 2025. Сборщик не добавляю.
        # Канал @freelansim_ru остаётся в sources.yaml: это Telegram, не сайт.
        # Тендерные площадки не подключаю.
        try:
            freelance_ru_items = await fetch_freelance_ru()
        except Exception:
            log.warning("Freelance.ru не отдал список заказов")
            freelance_ru_items = []
        for item in freelance_ru_items[:30]:
            if await _save(item, sources):
                saved += 1
        try:
            weblancer_items = await fetch_weblancer()
        except Exception:
            log.warning("Weblancer не отдал список заказов")
            weblancer_items = []
        for item in weblancer_items[:30]:
            if await _save(item, sources):
                saved += 1
        try:
            profi_items = await fetch_orders()
        except Exception:
            log.warning("Профи.ру не отдал ленту")
            profi_items = []
        for item in profi_items[:20]:
            if await _save(item, sources):
                saved += 1
        try:
            kwork_items = await fetch_kwork()
        except Exception:
            log.warning("Kwork не отдал список заказов")
            kwork_items = []
        for item in kwork_items[:24]:
            if await _save(item, sources):
                saved += 1
        try:
            avito_items = await fetch_avito()
        except Exception:
            log.warning("Авито Работа не отдал список вакансий")
            avito_items = []
        for item in avito_items[:200]:
            if await _save(item, sources):
                saved += 1
    log.info("Биржи добавили %s заявок", saved)
    return saved


async def poll_forever() -> None:
    while True:
        try:
            await poll_once()
        except Exception:
            log.exception("Сбор с бирж оборвался")
        await asyncio.sleep(PAUSE_SECONDS)
