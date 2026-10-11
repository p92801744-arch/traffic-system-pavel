"""Бот: карточка, кнопки, тихие часы, без повторов и без потерянного offset."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, notifier
from app.settings import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "bot_token", "")
    notifier._offset = 0
    notifier._pending_reject.clear()
    yield


def _beat(state: str, age: int = 0) -> dict:
    moment = datetime.now(timezone.utc) - timedelta(seconds=age)
    return {"state": state, "heartbeat_at": moment.isoformat(timespec="seconds"), "detail": ""}


async def _insert(**extra) -> dict:
    conn = await db.connect()
    try:
        lead_id = await db.upsert_lead(
            conn,
            {
                "source": extra.get("source", "fl"),
                "channel_username": extra.get("channel_username", "fl"),
                "channel_title": extra.get("channel_title", "FL"),
                "channel_id": extra.get("channel_id", "fl-1"),
                "message_id": extra.get("message_id", 1),
                "text": extra.get("text", "Нужен бот для магазина на n8n и каталог"),
                "url": extra.get("url", "https://www.fl.ru/projects/1/"),
                "kind": extra.get("kind", "order"),
                "write_url": extra.get("write_url"),
                "sender_name": extra.get("sender_name"),
            },
        )
        assert lead_id
        if extra.get("fit") is not None:
            await conn.execute(
                "UPDATE leads SET fit = ?, why = ?, status = ? WHERE id = ?",
                (extra["fit"], extra.get("why", "магазин и каталог"), extra.get("status", "new"), lead_id),
            )
            await conn.commit()
        if extra.get("created_at"):
            await conn.execute("UPDATE leads SET created_at = ? WHERE id = ?", (extra["created_at"], lead_id))
            await conn.commit()
        lead = await db.get_lead(conn, lead_id)
        assert lead
        return lead
    finally:
        await conn.close()


def test_card_escapes_once_and_names_the_source():
    text = notifier._format_lead(
        {
            "id": 1,
            "kind": "order",
            "source": "telegram",
            "channel_title": "A & B",
            "fit": 4,
            "high_ticket": 1,
            "why": "x < y & z",
            "text": "нужен каталог",
            "url": "https://t.me/a/1?q=1&x=2",
            "sender_name": "Иван",
        }
    )
    assert "&amp;amp;" not in text
    assert "A &amp; B" in text
    assert "x &lt; y &amp; z" in text
    assert "Оценка: 4/5, высокий чек" in text
    assert "Откуда: Telegram, A &amp; B" in text
    assert 'href="https://t.me/a/1?q=1&amp;x=2"' in text
    assert "Кто: Иван" in text


def test_keyboard_has_the_four_actions():
    kb = notifier._keyboard(
        {"id": 9, "url": "https://www.fl.ru/p/1", "source": "fl", "write_url": "https://www.fl.ru/u/1"}
    )
    flat = [button["text"] for row in kb["inline_keyboard"] for button in row]
    assert flat == ["Открыть", "Написать", "Черновик", "Не моё", "Позже", "Готово"]
    data = [button.get("callback_data") for row in kb["inline_keyboard"] for button in row]
    assert data[2:] == ["r:9", "n:9", "l:9", "d:9"]


def test_digest_stays_plain_text():
    text = notifier._digest_text(
        [{"fit": 4, "text": "A & B", "url": "https://t.me/a/1", "source": "fl", "channel_title": "FL"}]
    )
    assert "A & B" in text
    assert "&amp;" not in text
    assert "FL.ru" in text


def test_quiet_hours_cover_night_only():
    assert notifier.in_quiet_hours(datetime(2026, 10, 11, 22, 0))
    assert notifier.in_quiet_hours(datetime(2026, 10, 11, 7, 59))
    assert not notifier.in_quiet_hours(datetime(2026, 10, 11, 8, 0))
    assert not notifier.in_quiet_hours(datetime(2026, 10, 11, 21, 59))


def test_redact_hides_the_token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123456:ABCDEF")
    raw = "fail https://api.telegram.org/bot123456:ABCDEF/getUpdates"
    assert notifier._redact(raw) == "fail https://api.telegram.org/bot***/getUpdates"


def test_token_is_not_hardcoded():
    source = Path(notifier.__file__).read_text(encoding="utf-8")
    assert re.search(r"\d{6,}:[A-Za-z0-9_-]{20,}", source) is None


def test_poll_decision_stops_on_401_and_backs_off_on_409():
    assert notifier._poll_decision(401, {}) == "stop"
    assert notifier._poll_decision(200, {"ok": False, "description": "Unauthorized"}) == "stop"
    assert notifier._poll_decision(409, {"description": "Conflict: terminated by other getUpdates request"}) == "conflict"
    assert notifier._poll_decision(200, {"ok": False, "description": "Bad Gateway"}) == "retry"
    assert notifier._poll_decision(200, {"ok": True, "result": []}) == "ok"


def test_callback_text_fits_the_telegram_limit():
    assert len(notifier._callback_text("а" * 250)) <= notifier.CALLBACK_TEXT_LIMIT
    assert notifier._callback_text("коротко") == "коротко"


def test_offset_advances_only_after_handling(monkeypatch):
    seen: dict[str, int] = {}

    async def boom(update):
        seen["during"] = notifier._offset
        raise RuntimeError("boom https://api.telegram.org/bot123456:ABCDEF/getUpdates")

    monkeypatch.setattr(settings, "bot_token", "123456:ABCDEF")
    monkeypatch.setattr(notifier, "_handle_update", boom)

    async def go():
        notifier._offset = 4
        await notifier._consume_update({"update_id": 9, "message": {}})

    asyncio.run(go())
    assert seen["during"] == 4
    assert notifier._offset == 10
    assert notifier._load_offset() == 10


def test_pending_reason_survives_reload_and_ignores_commands(monkeypatch):
    calls: list[str] = []

    async def fake_post(method, payload):
        calls.append(payload.get("text") or "")
        return {}

    monkeypatch.setattr(notifier, "_post", fake_post)
    monkeypatch.setattr(notifier.lessons, "add_lesson", lambda *args, **kwargs: {"conclusion": "Запомнил.", "instruction": ""})

    async def go():
        lead = await _insert(fit=4, message_id=3, text="Нужен курьер на склад")
        notifier._pending_reject[7] = lead["id"]
        notifier._save_pending()
        notifier._pending_reject.clear()
        notifier._load_pending()
        assert notifier._pending_reject[7] == lead["id"]
        await notifier._handle_update({"message": {"chat": {"id": 7}, "text": "/help", "from": {}}})
        assert notifier._pending_reject[7] == lead["id"]
        await notifier._handle_update({"message": {"chat": {"id": 7}, "text": "это курьер", "from": {}}})
        assert 7 not in notifier._pending_reject
        conn = await db.connect()
        try:
            saved = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert saved["status"] == "reject"
        assert saved["reject_reason"] == "это курьер"
        return lead

    asyncio.run(go())
    assert any("Черновик" in text for text in calls)
    assert any("Запомнил." in text for text in calls)


def test_later_and_done_update_status(monkeypatch):
    async def fake_post(method, payload):
        return {}

    monkeypatch.setattr(notifier, "_post", fake_post)

    async def go():
        later = await _insert(fit=4, message_id=4, text="Сделать сайт магазина позже", status="take")
        done = await _insert(fit=5, message_id=5, text="Сделать связку магазина и 1С", channel_id="fl-2")
        await notifier._handle_callback({"id": "cb-l", "data": f"l:{later['id']}", "message": {"chat": {"id": 1}}})
        await notifier._handle_callback({"id": "cb-d", "data": f"d:{done['id']}", "message": {"chat": {"id": 1}}})
        conn = await db.connect()
        try:
            later_row = await db.get_lead(conn, later["id"])
            done_row = await db.get_lead(conn, done["id"])
        finally:
            await conn.close()
        assert later_row["status"] == "new"
        assert done_row["status"] == "contacted"

    asyncio.run(go())


def test_old_take_callback_still_starts_a_draft(monkeypatch):
    started: list[int] = []

    async def fake_post(method, payload):
        return {}

    async def fake_start(lead_id: int):
        started.append(lead_id)

    monkeypatch.setattr(notifier, "_post", fake_post)
    monkeypatch.setattr("app.reply.start_reply", fake_start)

    async def go():
        lead = await _insert(fit=4, message_id=6, text="Нужен конвейер карточек для магазина")
        await notifier._handle_callback({"id": "cb-a", "data": f"a:{lead['id']}", "message": {"chat": {"id": 1}}})
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        conn = await db.connect()
        try:
            saved = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert saved["status"] == "take"
        assert started == [lead["id"]]

    asyncio.run(go())


def test_send_lead_once_and_skip_quiet_hours(monkeypatch):
    sent: list[int] = []

    async def fake_post(method, payload):
        sent.append(payload["chat_id"])
        return {}

    monkeypatch.setattr(notifier, "_post", fake_post)
    monkeypatch.setattr(notifier, "enabled", lambda: True)

    async def go():
        conn = await db.connect()
        try:
            await db.add_subscriber(conn, 42, "pavel")
        finally:
            await conn.close()
        lead = await _insert(fit=4, why="каталог магазина", message_id=8, text="Собрать каталог магазина A & B")
        monkeypatch.setattr(notifier, "in_quiet_hours", lambda moment=None: True)
        await notifier.send_lead(lead)
        assert sent == []
        conn = await db.connect()
        try:
            quiet = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert quiet["bot_sent_at"] is None
        monkeypatch.setattr(notifier, "in_quiet_hours", lambda moment=None: False)
        await notifier.send_lead(lead)
        await notifier.send_lead(lead)
        assert sent == [42]
        conn = await db.connect()
        try:
            saved = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert saved["bot_sent_at"]

    asyncio.run(go())


def test_failed_send_can_be_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_post(method, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("https://api.telegram.org/bot123456:ABCDEF/sendMessage")
        return {}

    monkeypatch.setattr(notifier, "_post", fake_post)
    monkeypatch.setattr(notifier, "enabled", lambda: True)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda moment=None: False)
    monkeypatch.setattr(settings, "bot_token", "123456:ABCDEF")

    async def go():
        conn = await db.connect()
        try:
            await db.add_subscriber(conn, 42, "pavel")
        finally:
            await conn.close()
        lead = await _insert(fit=5, message_id=9, text="Нужна связка магазина с 1С")
        await notifier.send_lead(lead)
        conn = await db.connect()
        try:
            released = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert released["bot_sent_at"] is None
        await notifier.send_lead(lead)
        conn = await db.connect()
        try:
            saved = await db.get_lead(conn, lead["id"])
        finally:
            await conn.close()
        assert saved["bot_sent_at"]
        assert calls["n"] == 2

    asyncio.run(go())


def test_flush_sends_recent_unsent_only(monkeypatch):
    sent: list[str] = []

    async def fake_post(method, payload):
        sent.append(payload["text"])
        return {}

    monkeypatch.setattr(notifier, "_post", fake_post)
    monkeypatch.setattr(notifier, "enabled", lambda: True)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda moment=None: False)

    async def go():
        conn = await db.connect()
        try:
            await db.add_subscriber(conn, 7, "pavel")
        finally:
            await conn.close()
        await _insert(fit=4, message_id=10, text="Свежая заявка на каталог магазина")
        await _insert(
            fit=4,
            message_id=11,
            text="Старая заявка на каталог магазина",
            channel_id="fl-old",
            created_at="2020-01-01T00:00:00+00:00",
        )
        count = await notifier.flush_unsent_cards()
        assert count == 1
        assert len(sent) == 1
        assert "Свежая заявка" in sent[0]

    asyncio.run(go())


def test_watch_alerts_once_then_reports_recovery():
    stale = {"telegram": _beat("running", age=400)}
    state, messages = notifier.advance_watch({}, stale, now=0)
    assert messages == []
    state, messages = notifier.advance_watch(state, stale, now=notifier.WATCH_GRACE_SECONDS)
    assert len(messages) == 1
    assert "молчит" in messages[0]
    state, again = notifier.advance_watch(state, stale, now=notifier.WATCH_GRACE_SECONDS + 30)
    assert again == []
    state, recovered = notifier.advance_watch(state, {"telegram": _beat("running")}, now=1000)
    assert len(recovered) == 1
    assert "снова работает" in recovered[0]
    assert "telegram" not in state


def test_watch_ignores_missing_heartbeat_and_waits_out_network():
    state, messages = notifier.advance_watch({}, {"telegram": None, "boards": None, "networking": None}, now=10_000)
    assert state == {}
    assert messages == []
    waiting = {"boards": _beat("waiting_network")}
    state, messages = notifier.advance_watch({}, waiting, now=0)
    assert messages == []
    _state, messages = notifier.advance_watch(state, waiting, now=notifier.NETWORK_GRACE_SECONDS - 1)
    assert messages == []
    _state, messages = notifier.advance_watch(state, waiting, now=notifier.NETWORK_GRACE_SECONDS)
    assert len(messages) == 1
    assert "нет сети" in messages[0]


def test_status_lines_are_plain():
    assert "нужен вход" in notifier.describe_heartbeat("telegram", _beat("need_login"))
    assert "молчит" in notifier.describe_heartbeat("boards", _beat("running", age=400))
    assert "работает" in notifier.describe_heartbeat("networking", _beat("running"))
    assert "пульса нет" in notifier.describe_heartbeat("telegram", None)


def test_help_names_the_buttons():
    text = notifier._help_text()
    for word in ("Черновик", "Не моё", "Позже", "Готово", "/status", "/stats"):
        assert word in text
