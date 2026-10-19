"""Отклики из командной строки: список новых, отметка, отправка без дублей и с лимитами."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import db, outreach
from app.settings import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(
        outreach,
        "config",
        lambda: {**outreach.DEFAULTS, "min_interval_sec": 0, "max_per_hour": 2, "max_per_day": 5},
    )
    yield


def _lead(n: int, **extra) -> dict:
    base = {
        "source": "telegram",
        "channel_username": "smm_chat",
        "channel_title": "SMM чат",
        "channel_id": 777,
        "message_id": 100 + n,
        "message_date": "2026-10-19T08:00:00+00:00",
        "text": f"Ищу SMM-специалиста на ведение Instagram кофейни номер {n}, бюджет 60 000 ₽ в месяц. Подробности в личке.",
        "url": f"https://t.me/smm_chat/{100 + n}",
        "sender_username": f"client{n}",
        "sender_id": 5000 + n,
        "sender_name": f"Клиент {n}",
        "write_url": f"https://t.me/client{n}",
        "kind": "order",
    }
    base.update(extra)
    return base


async def _seed(conn, *leads) -> list[int]:
    ids = []
    for payload in leads:
        lead_id = await db.upsert_lead(conn, payload)
        assert lead_id
        await db.save_score(conn, lead_id, {"fit": 4, "is_paid_order": True, "why": "smm", "suggested_offer": "smm", "high_ticket": True})
        ids.append(lead_id)
    return ids


class FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, via, peer, lead, text):
        self.calls.append((via, peer, lead["id"], text))
        return str(900 + len(self.calls))


def test_list_new_returns_full_lead_json():
    async def go():
        conn = await outreach.open_db()
        try:
            (lead_id,) = await _seed(conn, _lead(1))
            rows = await outreach.list_new(conn)
        finally:
            await conn.close()
        assert [r["id"] for r in rows] == [lead_id]
        row = rows[0]
        assert "кофейни номер 1" in row["text"]
        assert row["source"] == "telegram"
        assert row["link"] == "https://t.me/smm_chat/101"
        assert row["author"]["username"] == "@client1"
        json.dumps(rows, ensure_ascii=False)

    asyncio.run(go())


def test_send_once_then_duplicate_is_refused():
    sender = FakeSender()

    async def go():
        conn = await outreach.open_db()
        try:
            (lead_id,) = await _seed(conn, _lead(1))
            result = await outreach.send_reply(conn, lead_id, "Добрый день! Ведём похожие аккаунты, расскажете про задачу?", sender=sender)
            assert result["state"] == "sent"
            assert result["via"] == "telegram-dm"
            assert result["peer"] == "client1"
            with pytest.raises(outreach.OutreachError):
                await outreach.send_reply(conn, lead_id, "Ещё раз тот же отклик, второй раз", sender=sender)
            lead = await db.get_lead(conn, lead_id)
            assert lead["status"] == "contacted"
            assert lead["outreach_state"] == "sent"
            assert lead["outreach_at"]
            assert "аккаунты" in lead["outreach_text"]
            assert await outreach.list_new(conn) == []
        finally:
            await conn.close()
        assert len(sender.calls) == 1

    asyncio.run(go())


def test_thread_mode_and_same_author_cooldown():
    sender = FakeSender()

    async def go():
        conn = await outreach.open_db()
        try:
            a, b, c = await _seed(
                conn,
                _lead(1),
                _lead(2, write_url="https://t.me/client1", sender_username="client1"),
                _lead(3, write_url=None),
            )
            await outreach.send_reply(conn, a, "Первый отклик автору, коротко", sender=sender)
            with pytest.raises(outreach.OutreachError, match="этому автору"):
                await outreach.send_reply(conn, b, "Второй отклик тому же автору", sender=sender)
            res = await outreach.send_reply(conn, c, "Ответ в треде чата под заявкой", sender=sender)
            assert res["via"] == "telegram-thread"
            assert res["peer"] == "smm_chat"
        finally:
            await conn.close()

    asyncio.run(go())


def test_hourly_limit_and_error_releases_claim():
    sender = FakeSender()

    async def boom(via, peer, lead, text):
        raise RuntimeError("chat write forbidden")

    async def go():
        conn = await outreach.open_db()
        try:
            ids = await _seed(conn, _lead(1), _lead(2), _lead(3), _lead(4))
            with pytest.raises(outreach.OutreachError, match="не отправилось"):
                await outreach.send_reply(conn, ids[0], "Отклик, который упадёт", sender=boom)
            lead = await db.get_lead(conn, ids[0])
            assert lead["outreach_state"] == "error"
            await outreach.send_reply(conn, ids[0], "Повтор после ошибки проходит", sender=sender)
            await outreach.send_reply(conn, ids[1], "Второй отклик за час", sender=sender)
            with pytest.raises(outreach.OutreachError, match="в час"):
                await outreach.send_reply(conn, ids[2], "Третий упирается в лимит", sender=sender)
        finally:
            await conn.close()

    asyncio.run(go())


def test_mark_replied_for_board_lead_and_dry_run():
    async def go():
        conn = await outreach.open_db()
        try:
            kwork_id, tg_id = await _seed(conn, _lead(1, source="kwork", channel_id="k1"), _lead(2))
            with pytest.raises(outreach.OutreachError, match="mark-replied"):
                await outreach.send_reply(conn, kwork_id, "Отклик на бирже не уходит через Telegram")
            done = await outreach.mark_replied(conn, kwork_id, "Отклик на Kwork", via="kwork")
            assert done["state"] == "marked"
            with pytest.raises(outreach.OutreachError):
                await outreach.mark_replied(conn, kwork_id, "Второй раз")
            dry = await outreach.send_reply(conn, tg_id, "Проверка без отправки", dry_run=True)
            assert dry["dry_run"] is True
            lead = await db.get_lead(conn, tg_id)
            assert lead["outreach_state"] is None
        finally:
            await conn.close()

    asyncio.run(go())


def test_skip_sets_junk_and_drops_out_of_list_new():
    async def seed():
        conn = await outreach.open_db()
        try:
            return await _seed(conn, _lead(1), _lead(2), _lead(3))
        finally:
            await conn.close()

    kept, first, second = asyncio.run(seed())
    assert outreach.main(["skip", str(first), str(second), "--reason", "исполнитель рекламирует себя"]) == 0

    async def check():
        conn = await outreach.open_db()
        try:
            assert [row["id"] for row in await outreach.list_new(conn)] == [kept]
            lead = await db.get_lead(conn, first)
            assert lead["status"] == "junk"
            assert lead["reject_reason"] == "исполнитель рекламирует себя"
            cur = await conn.execute(
                "SELECT via, ok, error FROM outreach_log WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
                (first,),
            )
            logged = await cur.fetchone()
            assert logged[0] == "skip" and logged[1] == 0 and "исполнитель" in logged[2]
            visible = await db.list_leads(conn, status="new", hide_low=False)
            assert first not in [row["id"] for row in visible]
            assert second not in [row["id"] for row in visible]
        finally:
            await conn.close()

    asyncio.run(check())
    assert outreach.main(["skip", "999999", "--reason", "нет такой заявки"]) == 2


def test_skip_requires_a_reason():
    with pytest.raises(SystemExit):
        outreach.main(["skip", "1"])


def test_stale_hides_old_unanswered_from_list_new():
    async def go():
        conn = await outreach.open_db()
        try:
            old_new, recent, old_take, old_sent = await _seed(conn, _lead(1), _lead(2), _lead(3), _lead(4))
            old = "2026-10-01T08:00:00+00:00"
            fresh = "2026-10-18T08:00:00+00:00"
            await conn.execute("UPDATE leads SET message_date = ?, created_at = ? WHERE id = ?", (old, old, old_new))
            await conn.execute("UPDATE leads SET message_date = ?, created_at = ? WHERE id = ?", (fresh, fresh, recent))
            await conn.execute(
                "UPDATE leads SET status = 'take', message_date = ?, created_at = ? WHERE id = ?",
                (old, old, old_take),
            )
            await conn.execute(
                "UPDATE leads SET outreach_state = 'sent', status = 'contacted', message_date = ?, created_at = ? WHERE id = ?",
                (old, old, old_sent),
            )
            await conn.commit()
            result = await outreach.mark_stale(conn, 5)
            assert result["days"] == 5
            assert set(result["stale"]) == {old_new, old_take}
            listed = {row["id"] for row in await outreach.list_new(conn)}
            assert listed == {recent}
            assert (await db.get_lead(conn, old_sent))["status"] == "contacted"
            assert (await db.get_lead(conn, old_new))["status"] == "stale"
        finally:
            await conn.close()

    asyncio.run(go())
    assert outreach.main(["stale", "--days", "5"]) == 0


def test_flood_wait_pauses_next_send():
    class Flood(Exception):
        seconds = 600

    async def flood(via, peer, lead, text):
        raise Flood("wait")

    async def go():
        conn = await outreach.open_db()
        try:
            a, b = await _seed(conn, _lead(1), _lead(2))
            with pytest.raises(outreach.OutreachError):
                await outreach.send_reply(conn, a, "Отклик под FloodWait", sender=flood)
            with pytest.raises(outreach.OutreachError, match="FloodWait"):
                await outreach.send_reply(conn, b, "Следующий ждёт паузу", sender=FakeSender())
        finally:
            await conn.close()

    asyncio.run(go())
