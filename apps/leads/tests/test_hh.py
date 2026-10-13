"""HeadHunter через RSS: удалёнка/проект, фильтр по названию, свежесть, шаблон не отпечаток."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import hh, lessons

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


def _rss(rows):
    items = "".join(
        f"<item><title>{t}</title><link>https://hh.ru/vacancy/{i}?from=rss</link>"
        f"<description>&lt;p&gt;Вакансия компании: Ромашка&lt;/p&gt;&lt;p&gt;Создана: 12.10.2026&lt;/p&gt;"
        f"&lt;p&gt;Регион: Москва&lt;/p&gt;&lt;p&gt;Предполагаемый уровень месячного дохода: от 80 000 ₽&lt;/p&gt;</description>"
        f"<pubDate>{d}</pubDate></item>"
        for i, t, d in rows
    )
    return f"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>{items}</channel></rss>"


def test_items_title_filter_freshness_and_text():
    xml = _rss([
        (101, "SMM-менеджер", "2026-10-12T10:00:00+03:00"),
        (102, "Продавец-консультант", "2026-10-12T10:00:00+03:00"),
        (103, "Инженер по автоматизации тестирования (Java)", "2026-10-12T10:00:00+03:00"),
        (104, "SEO-специалист", "2026-10-01T10:00:00+03:00"),
    ])
    found = hh.items_from_rss(xml, "Удалённо", NOW)
    assert [row["message_id"] for row in found] == [101]
    text = found[0]["text"]
    assert text.startswith("SMM-менеджер. Компания: Ромашка")
    assert "Доход: от 80 000 ₽" in text and text.endswith("Удалённо")
    assert "Создана" not in text
    assert found[0]["url"] == "https://hh.ru/vacancy/101"


def test_remote_and_project_modes_and_queries():
    params = [p for p, _ in hh.MODES]
    assert {"work_format": "REMOTE"} in params and {"employment": "project"} in params
    assert {"n8n", "python", "smm", "seo"} <= set(hh.QUERIES)


def test_hh_template_is_not_a_repost_fingerprint(tmp_path, monkeypatch):
    path = tmp_path / "l.json"
    about = "Продавец-консультант. Компания: Снежная Королева Регион: Москва Доход: не указан. Удалённо"
    path.write_text(json.dumps({"lessons": [{"reason": "не наше", "about": about}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(lessons, "LESSONS_PATH", path)
    assert lessons.learned_block(about)
    other = "SMM-менеджер. Компания: Яндекс Регион: Москва Доход: не указан. Удалённо"
    assert lessons.learned_block(other) is None
