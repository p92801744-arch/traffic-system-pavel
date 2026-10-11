"""Маршруты панели: безопасный next, фильтры ленты, 404 на несуществующую заявку."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import collector, lessons, main, reply
from app.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_ROLE", "web")
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "telegram_api_id", 1)
    monkeypatch.setattr(settings, "telegram_api_hash", "x")
    monkeypatch.setattr(lessons, "LESSONS_PATH", tmp_path / "lessons.json")

    async def yes(*a, **k):
        return True

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(collector, "is_authorized", yes)
    monkeypatch.setattr(collector, "disconnect", nothing)
    monkeypatch.setattr(reply, "start_reply", nothing)
    monkeypatch.setattr(main, "_rescore_safe", nothing)
    with TestClient(main.app) as c:
        yield c


def _add(status="new", fit=4, text="Ищу разработчика чат-бота") -> int:
    conn = sqlite3.connect(settings.db_path)
    cur = conn.execute(
        "INSERT INTO leads (text, status, fit, created_at, message_id) VALUES (?, ?, ?, '2026-10-12', abs(random()))",
        (text, status, fit),
    )
    conn.commit()
    lead_id = cur.lastrowid
    conn.close()
    return lead_id


def _status(lead_id: int) -> str | None:
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.mark.parametrize("bad", ["https://evil.example", "//evil.example", "/\\\\evil.example", "evil"])
def test_next_open_redirect_blocked(client, bad):
    lead_id = _add()
    r = client.post(f"/leads/{lead_id}/status", data={"status": "contacted", "next": bad}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post(f"/leads/{lead_id}/reject", data={"reason": "не моё", "next": bad}, follow_redirects=False)
    assert r.headers["location"] == "/"
    r = client.post(f"/leads/{lead_id}/delete", data={"next": bad}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_next_relative_path_kept(client):
    lead_id = _add()
    r = client.post(f"/leads/{lead_id}/status", data={"status": "contacted", "next": "/taken"}, follow_redirects=False)
    assert r.headers["location"] == "/taken"
    assert _status(lead_id) == "contacted"


def test_safe_chats_next_exact_prefix():
    assert main._safe_chats_next("/chats") == "/chats"
    assert main._safe_chats_next("/chats?sort=spam&dir=asc") == "/chats?sort=spam&dir=asc"
    assert main._safe_chats_next("/chats/x") == "/chats/x"
    assert main._safe_chats_next("/chats.evil.example") == "/chats"
    assert main._safe_chats_next("/chatsfoo") == "/chats"
    assert main._safe_chats_next("https://evil") == "/chats"


def test_feed_honors_status_and_show_low(client):
    new_id = _add(text="НОВАЯ_ЗАЯВКА")
    _add(status="take", text="ВЗЯТАЯ_ЗАЯВКА")
    _add(fit=1, text="СЛАБАЯ_ЗАЯВКА")
    page = client.get("/").text
    assert "НОВАЯ_ЗАЯВКА" in page and "ВЗЯТАЯ_ЗАЯВКА" not in page
    assert "СЛАБАЯ_ЗАЯВКА" in page  # по умолчанию как раньше: слабые видны
    page = client.get("/?status=take").text
    assert "ВЗЯТАЯ_ЗАЯВКА" in page and "НОВАЯ_ЗАЯВКА" not in page
    assert "СЛАБАЯ_ЗАЯВКА" not in client.get("/?show_low=0").text
    assert "СЛАБАЯ_ЗАЯВКА" in client.get("/?show_low=1").text
    assert "НОВАЯ_ЗАЯВКА" in client.get("/?status=bogus").text
    assert new_id


def test_missing_lead_returns_404(client):
    for url, data in (
        ("/leads/999999/status", {"status": "take"}),
        ("/leads/999999/reject", {"reason": "не моё"}),
        ("/leads/999999/delete", {}),
        ("/leads/999999/reply", {"again": "1"}),
    ):
        r = client.post(url, data=data, follow_redirects=False)
        assert r.status_code == 404, url
