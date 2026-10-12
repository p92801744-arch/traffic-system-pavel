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
    assert {item[0] for item in plan} == {"all", "sankt-peterburg"}
    nxt = avito.plan_run({"offset": avito.PER_REGION}, now)
    assert plan[0][3] != nxt[0][3]
    assert avito.plan_run({"last_run": (now - timedelta(minutes=10)).isoformat()}, now) is None
    assert avito.plan_run({"blocked_at": (now - timedelta(hours=1)).isoformat()}, now) is None
    assert avito.plan_run({"last_run": (now - timedelta(hours=2)).isoformat()}, now)


def test_queries_avoid_known_redirects():
    bad = {"n8n", "tilda", "python", "чат-бот", "парсинг", "нейросети", "ии", "rpa", "chatgpt", "администратор сайта"}
    assert not bad & {query for _, query in avito.QUERIES}
    assert 20 <= len(avito.QUERIES) <= 32
    # Весь список за 4 захода.
    assert len(avito.QUERIES) <= avito.PER_REGION * 4


def test_smm_and_staff_vacancy_cards_pass():
    sources = load_sources()
    for title in (
        "SMM менеджер. 50 000 ₽ на руки",
        "Контент менеджер соцсетей. от 40 000 ₽",
        "Таргетолог по VK Ads. 60 000 — 90 000 ₽",
        "Копирайтер. 45 000 ₽ на руки",
        "SEO-специалист. 70 000 ₽",
        "Специалист по внедрению ии в бизнес. 100 000 ₽",
        "Интернет маркетолог. 80 000 ₽",
        "Маркетолог/SMM-специалист. 70 000 ₽",
    ):
        assert intake_hits(title + ". " + title + TAIL, sources, assume_request=True), title


def test_marketing_section_url_and_spb_mirror():
    url = avito._search_url("smm", "sankt-peterburg", avito.MARKETING)
    assert url.startswith("https://www.avito.ru/sankt-peterburg/vakansii/marketing_reklama_i_pr-")
    assert avito.in_vacancies(url)
    assert (avito.MARKETING, "маркетолог") in avito.QUERIES


def test_title_filter_cuts_noise_keeps_fits():
    for good in (
        "Программист 1С Middle+/Senior", "Специалист по внедрению ии в Бизнес-процессы",
        "Fullstack разработчик", "SMM специалист", "Интернет маркетолог", "Контент менеджер сайта",
        "Разработчик чат-ботов (Без опыта/Обучение)", "Таргетолог по VK Ads", "Руководитель отдела разработки 1С", "Менеджер по работе с сайтом", "Специалист по трафику", "Программист сбис (Saby) /Интегратор CRM",
    ):
        assert avito.title_relevant(good), good
    for bad in (
        "Заведующий аптекой (м. Давыдково)", "Инженер-проектировщик асу тп / кипиа",
        "Менеджер по привлечению корпоративных клиентов", "Специалист охраны", "Курьер Водитель в Яндекс Еда",
        "Менеджер по продажам B2B / B2C", "Спец по AI, платное обучение с нуля", "Персональный банкир",
    ):
        assert not avito.title_relevant(bad), bad


def test_designers_and_marketplace_managers_still_rejected():
    from app.filters import not_my_job

    assert not_my_job("Менеджер маркетплейсов. 70 000 ₽. Менеджер маркетплейсов" + TAIL)
    assert not_my_job("Дизайнер карточек товаров на маркетплейсы. 50 000 ₽" + TAIL)
    assert not_my_job("Графический дизайнер удаленно. 50 000 ₽" + TAIL)
