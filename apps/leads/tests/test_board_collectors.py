"""Сборщики бирж: техработы FL, Weblancer через браузер, Профи 401, уроки про штат."""
from __future__ import annotations

import asyncio
import json

import httpx

import app.avito as avito
from app import boards, lessons, profi, weblancer

MAINT = "<html><title>FL</title><body>На сайте проводятся технические работы.</body></html>"
CF = "<html><head><title>Just a moment...</title></head><body>challenge-platform</body></html>"
WL = (
    '<h2 class="text-xl"><a href="/freelance/sajty/sdelat-katalog-123456/">Каталог для магазина</a></h2>'
    '<p class="text-gray-600">Нужен сайт-каталог на WordPress с выгрузкой товаров</p>'
    '<span class="text-green-600">50 000 ₽</span><span>12.10.2026</span>'
)


def _mock_client(monkeypatch, module, handler):
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", fake_client)


def test_fl_maintenance_detected():
    assert boards.fl_maintenance(MAINT)
    assert boards._fl_items(MAINT) == []
    assert not boards.fl_maintenance("<html>обычная лента</html>")


def test_weblancer_falls_back_to_browser(monkeypatch):
    _mock_client(monkeypatch, weblancer, lambda r: httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"}))

    async def fake_html(url, **_):
        assert url == weblancer.BOARD_URL
        return WL

    monkeypatch.setattr(avito, "browser_html", fake_html)
    items = asyncio.run(weblancer.fetch_orders())
    assert [item["message_id"] for item in items] == [123456]
    assert "WordPress" in items[0]["text"]


def test_weblancer_browser_also_blocked(monkeypatch):
    async def fake_html(url, **_):
        return CF

    monkeypatch.setattr(avito, "browser_html", fake_html)
    assert asyncio.run(weblancer._via_browser()) == []


def test_profi_expired_login_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(profi, "_cookies", lambda: {"sid": "x"})

    async def no_refresh():
        return False

    monkeypatch.setattr(profi, "refresh_from_browser", no_refresh)
    _mock_client(monkeypatch, profi, lambda r: httpx.Response(401, json={}))
    with caplog.at_level("WARNING"):
        assert asyncio.run(profi.fetch_orders()) == []
    assert "вход истёк" in caplog.text


def test_staff_lesson_no_longer_blocks_and_avito_template_ignored(tmp_path, monkeypatch):
    path = tmp_path / "lessons.json"
    staff_about = (
        "Помощник руководителя / менеджер проектов. от 110 000 ₽ на руки. "
        "Отклик с резюме Фиксированный график · Выплаты Раз в неделю"
    )
    promo_about = "Топовый впн прокси, называй как хочешь, подключайся к нашему боту прямо сейчас бесплатно"
    rows = [
        {"reason": "штатная вакансия, ищут сотрудника", "about": staff_about},
        {"reason": "реклама", "about": promo_about},
    ]
    path.write_text(json.dumps({"lessons": rows}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(lessons, "LESSONS_PATH", path)
    assert lessons.learned_block(staff_about) is None
    other = (
        "Программист 1С Middle+/Senior. Зарплата не указана. "
        "Отклик с резюме Фиксированный график · Выплаты Раз в неделю"
    )
    assert lessons.learned_block(other) is None
    assert lessons.learned_block(promo_about)


def test_profi_refreshes_login_from_browser_on_401(monkeypatch, tmp_path):
    session = tmp_path / "profi-session.json"
    session.write_text(json.dumps([{"host": ".profi.ru", "name": "sid", "value": "old"}]), encoding="utf-8")
    monkeypatch.setattr(profi, "SESSION_PATH", session)

    async def fresh(url):
        return [
            {"host": ".profi.ru", "name": "prfr_bo_tkn", "value": "new"},
            {"host": ".other.ru", "name": "x", "value": "y"},
        ]

    monkeypatch.setattr(avito, "browser_cookies", fresh)
    board = {"data": {"boSearchBoardItems": {"items": [{"id": "77", "title": "Сайт", "isFresh": True}]}}}

    def handler(request):
        if "prfr_bo_tkn=new" in request.headers.get("cookie", ""):
            return httpx.Response(200, json=board)
        return httpx.Response(401, json={})

    _mock_client(monkeypatch, profi, handler)
    items = asyncio.run(profi.fetch_orders())
    assert [item["message_id"] for item in items] == [77]
    saved = json.loads(session.read_text(encoding="utf-8"))
    assert [row["name"] for row in saved] == ["prfr_bo_tkn"]
