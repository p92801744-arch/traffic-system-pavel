"""Широкий поиск Авито: только Работа/Вакансии, ротация запросов, регионы, SMM не режется."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import avito
from app.filters import intake_hits, load_sources

TAIL = " Отклик с резюме Гибкий график · Выплаты Раз в месяц · Опыт более года"


def test_only_vacancy_category_urls():
    assert avito.in_vacancies("https://www.avito.ru/all/vakansii?q=smm&s=104")
    assert avito.in_vacancies("https://www.avito.ru/sankt-peterburg/vakansii?q=smm")
    assert avito.in_vacancies("https://www.avito.ru/all/vakansii/tag/targetolog?s=104")
    assert not avito.in_vacancies("https://www.avito.ru/all/predlozheniya_uslug?q=n8n")
    assert not avito.in_vacancies("https://www.avito.ru/all?q=python&s=104")
    assert avito._search_url("smm", "sankt-peterburg").startswith("https://www.avito.ru/sankt-peterburg/vakansii?q=smm")


def test_orders_drop_service_links_and_mark_city():
    items = [
        {"id": "1234567890", "title": "SMM менеджер", "href": "/sankt-peterburg/vakansii/smm_1234567890", "text": "x"},
        {"id": "1234567891", "title": "Настрою n8n", "href": "/moskva/predlozheniya_uslug/n8n_1234567891", "text": "x"},
        {"id": "1234567892", "title": "Что-то", "href": "/moskva/kvartiry/x_1234567892", "text": "x"},
    ]
    found = avito._orders(items, "Авито Работа · СПб")
    assert [item["message_id"] for item in found] == [1234567890]
    assert found[0]["channel_title"] == "Авито Работа · СПб"


def test_plan_rotates_and_respects_cooldowns():
    now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    plan = avito.plan_run({}, now)
    assert len(plan) == avito.PER_REGION * len(avito.REGIONS)
    assert {region for region, _, _ in plan} == {"all", "sankt-peterburg"}
    nxt = avito.plan_run({"offset": avito.PER_REGION}, now)
    assert plan[0][2] != nxt[0][2]
    assert avito.plan_run({"last_run": (now - timedelta(minutes=10)).isoformat()}, now) is None
    assert avito.plan_run({"blocked_at": (now - timedelta(hours=1)).isoformat()}, now) is None
    assert avito.plan_run({"last_run": (now - timedelta(hours=2)).isoformat()}, now)


def test_queries_avoid_known_redirects():
    bad = {"n8n", "tilda", "python", "чат-бот", "парсинг", "нейросети", "ии"}
    assert not bad & set(avito.QUERIES)
    assert 15 <= len(avito.QUERIES) <= 25


def test_smm_and_staff_vacancy_cards_pass():
    sources = load_sources()
    for title in (
        "SMM менеджер. 50 000 ₽ на руки",
        "Контент менеджер соцсетей. от 40 000 ₽",
        "Таргетолог по VK Ads. 60 000 — 90 000 ₽",
        "Копирайтер. 45 000 ₽ на руки",
        "SEO-специалист. 70 000 ₽",
        "Специалист по внедрению ии в бизнес. 100 000 ₽",
    ):
        assert intake_hits(title + ". " + title + TAIL, sources, assume_request=True), title
