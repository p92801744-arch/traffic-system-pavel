"""Сборщики бирж и страница нетворкинга."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from app import db, networking
from app.collector import watched_channels
from app.filters import (
    candidate_channels,
    configured_channels,
    intake_hits,
    is_offer_card,
    load_sources,
    not_my_job,
)
from app.freelance_ru import _orders as freelance_orders
from app.settings import settings
from app.weblancer import _orders as weblancer_orders

TODAY = date(2026, 10, 11)

FREELANCE_HTML = """
<article class="task-card">
  <a class="task-card__title-link" href="/task/view/12449" title="Сайт каталога">
    Разработка каталога для интернет-магазина
  </a>
  <p class="task-card__desc">Нужен каталог товаров и выгрузка на сайт. Бюджет обсуждается.</p>
  <div class="task-card__foot"><span class="task-card__foot-item" title="11.10.2026 12:00">сегодня</span></div>
  <div class="task-card__budget"><span class="bold">90 000 &#8381;</span> <span class="cost-unit">/ заказ</span></div>
</article>
<article class="task-card">
  <a class="task-card__title-link" href="/task/view/12450">Коротко</a>
  <p class="task-card__desc">Мало текста.</p>
</article>
"""

WEBLANCER_HTML = """
<h2 class="text-xl font-semibold capitalize-first"><a class="link-style" href="/freelance/sozdanie-saitov/katalog-internet-magazina-1300001/">Каталог интернет-магазина</a></h2>
<span class="text-green-600 text-xl whitespace-nowrap">90000<!-- --> ₽</span>
<p class="text-gray-600 flex-grow">Нужно собрать каталог товаров и выгрузить карточки на сайт. Срок две недели.</p>
<div class="flex flex-wrap items-center gap-4"><div><span>11.10.2026</span></div></div>
<h2 class="text-xl font-semibold"><a href="https://www.weblancer.net/freelance/1c/svyazka-s-moy-sklad-1300002/">Связка 1С и МойСклад</a></h2>
<p class="text-gray-600">Нужна автоматизация обмена заказами между 1С и МойСклад для интернет-магазина.</p>
<span>10.10.2026</span>
"""

MEROP_HTML = """
<article class="event-card">
  <a href="https://example.test/events/devoops-2026">
    <h2>DevOops 2026</h2>
    <p>Конференция по инженерным решениям</p>
    <span>12–13 октября</span>
    <span>Санкт-Петербург</span>
    <span>Офлайн</span>
  </a>
</article>
<article class="event-card is-muted">
  <a href="https://example.test/events/old">
    <h2>Старый митап</h2>
    <span>1 октября</span>
    <span>Санкт-Петербург</span>
  </a>
</article>
<article class="event-card">
  <a href="https://example.test/events/breakfast">
    <h2>Бизнес-завтрак основателей</h2>
    <p>Встреча предпринимателей</p>
    <span>20 октября</span>
    <span>Санкт-Петербург</span>
  </a>
</article>
"""

TG_HTML = """
<div data-post="bizspbnews/2085">
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Закрытый бизнес-завтрак</b><br/>2 октября собираемся с предпринимателями.</div>
  <div class="tgme_widget_message_footer"></div>
</div>
<div data-post="bizspbnews/2100">
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Бизнес-завтрак продавцов</b><br/>18 октября в Петербурге встреча для предпринимателей, кто продаёт на маркетплейсах.</div>
  <div class="tgme_widget_message_footer"></div>
</div>
<div data-post="bizspbnews/2101">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Круглый стол про доступную среду в городе и пандусы у подъездов, без продаж.</div>
</div>
<div data-post="bizspbnews/2106">
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>🎤</b> <b>Нетворкинг в Петербурге</b><br/>15 октября открытая встреча предпринимателей.</div>
</div>
"""

TIMEPAD = {
    "values": [
        {
            "id": 77,
            "name": "Митап продавцов маркетплейсов",
            "description_short": "Селлеры Wildberries и Ozon",
            "starts_at": "2026-10-18T10:00:00+03:00",
            "url": "https://example.test/timepad/77",
            "location": {"city": "Санкт-Петербург", "address": "Невский 1"},
        },
        {
            "id": 78,
            "name": "Концерт во дворе",
            "description_short": "Музыка и танцы",
            "starts_at": "2026-10-19T19:00:00+03:00",
            "url": "https://example.test/timepad/78",
            "location": {"city": "Санкт-Петербург", "address": "двор"},
        },
        {
            "id": 79,
            "name": "Митап в Москве",
            "description_short": "IT конференция",
            "starts_at": "2026-10-21T10:00:00+03:00",
            "url": "https://example.test/timepad/79",
            "location": {"city": "Москва", "address": "центр"},
        },
    ]
}

ORDER = (
    "Ищу подрядчика на каталог интернет-магазина и карточки товаров. "
    "Нужно выгрузить сайт. Бюджет 90 000 ₽."
)
TENDER = (
    "Ищу исполнителя на участие в закупке по 44-ФЗ. "
    "Нужен сайт и конкурсная документация. Бюджет 90 000 ₽."
)
PROCUREMENT = (
    "Ищу подрядчика на автоматизацию закупок в 1С для интернет-магазина. "
    "Бюджет 80 000 ₽, срок месяц."
)
OFFER = "Сделаю каталог и сайт под ключ. Принимаю заказы, мои услуги. От 15 000 ₽ за проект."


def test_candidate_chats_stay_out_of_the_listener():
    sources = load_sources()
    names = {item["username"].lower() for item in configured_channels()}
    watched = {((item.get("username") or "").lstrip("@")).lower() for item in watched_channels(sources)}
    candidates = candidate_channels()
    assert "freelansim_ru" in names
    assert "biznes_chat" not in names
    assert "biznes_chat" not in watched
    assert any(item["username"] == "biznes_chat" and item["why"] and item["url"].endswith("/biznes_chat") for item in candidates)
    assert all(item["why"] for item in candidates)
    assert "n8n_ru" in names


def test_freelance_ru_parses_client_order():
    items = freelance_orders(FREELANCE_HTML)
    assert len(items) == 2
    order = items[0]
    assert order["source"] == "freelance_ru"
    assert order["channel_id"] == "12449"
    assert order["url"] == "https://freelance.ru/task/view/12449"
    assert "90 000" in order["text"]
    assert "каталог" in order["text"].lower()
    assert not is_offer_card(order["text"])
    assert intake_hits(order["text"], load_sources(), assume_request=True)


def test_weblancer_parses_orders_and_skips_challenge():
    items = weblancer_orders(WEBLANCER_HTML)
    assert [item["channel_id"] for item in items] == ["1300001", "1300002"]
    assert items[0]["url"].endswith("/katalog-internet-magazina-1300001/")
    assert "1С" in items[1]["text"] or "1с" in items[1]["text"].lower()
    assert intake_hits(items[1]["text"], load_sources(), assume_request=True)
    assert weblancer_orders("<html><title>Just a moment...</title></html>") == []


def test_same_board_gates_reject_tender_and_service_card():
    sources = load_sources()
    assert intake_hits(ORDER, sources, assume_request=True)
    assert not_my_job(TENDER) == "тендер"
    assert intake_hits(TENDER, sources, assume_request=True) is None
    assert is_offer_card(OFFER)
    assert intake_hits(OFFER, sources, assume_request=True) is None
    assert not_my_job(PROCUREMENT) is None
    assert intake_hits(PROCUREMENT, sources, assume_request=True)


def test_no_tender_boards_in_sources():
    raw = Path(settings.sources_path).read_text(encoding="utf-8").lower()
    for banned in ("zakupki.gov", "b2b-center", "росэлторг", "сбербанк-аст", "тендер"):
        assert banned not in raw


def test_networking_parsers_drop_past_and_irrelevant():
    merop = networking.parse_merop(MEROP_HTML, TODAY)
    titles = [item["title"] for item in merop]
    assert "DevOops 2026" in titles
    assert "Бизнес-завтрак основателей" in titles
    assert "Старый митап" not in titles
    assert all(item["starts_on"] >= "2026-10-11" for item in merop)
    assert all(item["place"] and item["url"] and item["why"] and item["topic"] for item in merop)

    posts = networking.parse_telegram(TG_HTML, "bizspbnews", TODAY, assume_relevant=False)
    assert [item["external_id"] for item in posts] == ["2100", "2106"]
    assert posts[0]["starts_on"] == "2026-10-18"
    assert posts[1]["title"] == "Нетворкинг в Петербурге"
    assert "маркетплейс" in posts[0]["why"]

    pad = networking.parse_timepad(TIMEPAD, TODAY)
    assert [item["external_id"] for item in pad] == ["77"]
    assert "Невский" in pad[0]["place"]


def test_events_dedupe_and_drop_past(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "events.db")

    async def scenario():
        first = networking.parse_merop(MEROP_HTML, TODAY)[0]
        twin = dict(first)
        twin["source"] = "tg:bizspbnews"
        twin["external_id"] = "999"
        twin["url"] = "https://t.me/bizspbnews/999"
        past = dict(first)
        past["external_id"] = "past"
        past["title"] = "Уже прошло"
        past["title_key"] = networking.title_key(past["title"])
        past["starts_on"] = "2026-10-01"
        conn = await db.connect()
        try:
            assert await db.upsert_event(conn, first) is True
            assert await db.upsert_event(conn, first) is True
            assert await db.upsert_event(conn, twin) is False
            assert await db.upsert_event(conn, past) is True
            await db.delete_past_events(conn, TODAY.isoformat())
            rows = await db.list_events(conn, TODAY.isoformat())
        finally:
            await conn.close()
        assert [row["title"] for row in rows] == ["DevOops 2026"]
        assert rows[0]["url"] == first["url"]

    asyncio.run(scenario())


def test_networking_page_lists_event_and_chat_to_join(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "page.db")
    event = networking.parse_merop(MEROP_HTML, TODAY)[0]

    async def prepare():
        conn = await db.connect()
        try:
            await db.upsert_event(conn, event)
            past = dict(event)
            past["external_id"] = "gone"
            past["title"] = "Прошедшая конференция"
            past["title_key"] = networking.title_key(past["title"])
            past["starts_on"] = "2026-09-01"
            await db.upsert_event(conn, past)
        finally:
            await conn.close()

    asyncio.run(prepare())
    monkeypatch.setattr(networking, "date", _FrozenDate)
    # Импорт ленты тянет сборщик Авито, ему нужен путь профиля Windows.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    from fastapi.testclient import TestClient

    from app import boards, collector, notifier
    from app.main import app

    async def _quiet():
        return None

    async def _not_signed_in():
        return False

    monkeypatch.setattr(boards, "poll_forever", _quiet)
    monkeypatch.setattr(networking, "poll_forever", _quiet)
    monkeypatch.setattr(notifier, "start", _quiet)
    monkeypatch.setattr(notifier, "stop", _quiet)
    monkeypatch.setattr(collector, "is_authorized", _not_signed_in)

    with TestClient(app) as client:
        response = client.get("/networking")
    assert response.status_code == 200
    page = response.text
    assert "Нетворкинг" in page
    assert 'href="/networking"' in page
    assert "DevOops 2026" in page
    assert "Прошедшая конференция" not in page
    assert "Чаты, куда вступить самому" in page
    assert "t.me/biznes_chat" in page
    assert event["why"] in page


class _FrozenDate(date):
    @classmethod
    def today(cls) -> date:
        return TODAY
