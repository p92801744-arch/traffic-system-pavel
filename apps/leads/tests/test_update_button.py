"""Кнопка «Обновить ящик», лента непросмотренных, отклик сам только для Kwork."""
from __future__ import annotations

import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import collector, lessons, main, reply, updater
from app.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_ROLE", "web")
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "telegram_api_id", 1)
    monkeypatch.setattr(settings, "telegram_api_hash", "x")
    monkeypatch.setattr(lessons, "LESSONS_PATH", tmp_path / "lessons.json")
    started: list[int] = []

    async def yes(*a, **k):
        return True

    async def nothing(*a, **k):
        return None

    async def fake_start(lead_id):
        started.append(lead_id)

    async def no_collect():
        return None

    monkeypatch.setattr(collector, "is_authorized", yes)
    monkeypatch.setattr(collector, "disconnect", nothing)
    monkeypatch.setattr(collector, "author_profile", lambda lead: _profile())
    monkeypatch.setattr(reply, "start_reply", fake_start)
    monkeypatch.setattr(main, "_rescore_safe", nothing)
    monkeypatch.setattr(main, "_collect_manual", no_collect)
    with TestClient(main.app) as c:
        c.started = started
        yield c


async def _profile():
    return {"about": "", "kind": ""}


def _add(source="telegram", status="new", fit=4, url="https://t.me/x/1") -> int:
    conn = sqlite3.connect(settings.db_path)
    cur = conn.execute(
        "INSERT INTO leads (text, status, fit, created_at, message_id, source, url) "
        "VALUES ('Нужен бот на n8n', ?, ?, '2026-10-13', abs(random()), ?, ?)",
        (status, fit, source, url),
    )
    conn.commit()
    lead_id = cur.lastrowid
    conn.close()
    return lead_id


def _seen(lead_id):
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT seen_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return row[0]


def test_fresh_feed_shows_only_untouched_and_open_marks_seen(client):
    a, b = _add(), _add()
    _add(status="take")
    page = client.get("/?fresh=1").text
    assert "Не смотрел 2" in page and "Обновить ящик" in page
    r = client.get(f"/leads/{a}/open", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://t.me/x/1"
    assert _seen(a) and not _seen(b)
    assert "Не смотрел 1" in client.get("/?fresh=1").text
    client.post(f"/leads/{b}/reject", data={"reason": "не моё"})
    assert "Не смотрел 0" in client.get("/?fresh=1").text


def test_take_auto_reply_only_for_kwork(client):
    tg, kw = _add(), _add(source="kwork", url="https://kwork.ru/projects/1")
    client.post(f"/leads/{tg}/status", data={"status": "take"})
    client.post(f"/leads/{kw}/status", data={"status": "take"})
    assert client.started == [kw]
    client.get(f"/leads/{tg}/reply")
    assert client.started == [kw]
    assert _seen(tg)


def test_update_no_new_code_collects(client, monkeypatch):
    monkeypatch.setattr(updater, "check", lambda: {"state": "none", "behind": 0, "message": ""})
    spawned = []
    monkeypatch.setattr(updater, "spawn_restart", lambda req: spawned.append(req))
    r = client.post("/update")
    assert r.status_code == 200 and "Собираю заявки" in r.text and spawned == []


def test_update_with_new_code_pulls_and_restarts(client, monkeypatch):
    monkeypatch.setattr(updater, "check", lambda: {"state": "ahead", "behind": 2, "message": ""})
    monkeypatch.setattr(updater, "pull", lambda: {"ok": True, "requirements": True, "message": "Обновлено"})
    spawned = []
    monkeypatch.setattr(updater, "spawn_restart", lambda req: spawned.append(req))
    r = client.post("/update")
    assert "Обновляюсь" in r.text and spawned == [True]


def test_update_refuses_on_conflict(client, monkeypatch):
    monkeypatch.setattr(updater, "check", lambda: {"state": "ahead", "behind": 1, "message": ""})
    monkeypatch.setattr(updater, "pull", lambda: {"ok": False, "requirements": False, "message": "конфликт"})
    spawned = []
    monkeypatch.setattr(updater, "spawn_restart", lambda req: spawned.append(req))
    r = client.post("/update", follow_redirects=False)
    assert r.status_code == 303 and "fresh=1" in r.headers["location"] and spawned == []


def _cp(out="", code=0):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


def test_updater_check_and_pull(monkeypatch):
    calls = []

    def fake_git(*args, timeout=60):
        calls.append(args)
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return _cp("master\n")
        if args[0] == "rev-list":
            return _cp("3\n")
        if args[0] == "pull":
            return _cp("", 1)
        return _cp("abc\n")

    monkeypatch.setattr(updater, "_git", fake_git)
    assert updater.check() == {"state": "ahead", "behind": 3, "message": ""}
    result = updater.pull()
    assert result["ok"] is False and "принудительно не трогаю" in result["message"]
    assert all("--force" not in a and "reset" not in a for a in calls)
    assert ("pull", "--ff-only", "origin", "master") in calls