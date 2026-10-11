"""Рейтинг чатов для визитки: правила, спам, план и страница. Отправки нет."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError, UserNotParticipantError

from app import chat_rating, chat_scan, db, tgstat
from app.session_gate import ensure, plan_run, release, session_sqlite_busy
from app.settings import settings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def test_source_list_keeps_candidates_and_id_only():
    chats = chat_rating.source_chats(
        {
            "channels": [
                {"username": "Shop", "title": "Магазин"},
                {"username": "biznes_chat", "title": "Бизнес", "join": "candidate"},
                {"channel_id": 1615175239, "title": "Склад курсов"},
                {"title": "пусто"},
            ]
        }
    )
    keys = [item["chat_key"] for item in chats]
    assert keys == ["shop", "biznes_chat", "id-1615175239"]
    assert chats[1]["join"] == "candidate"


def test_real_sources_include_candidate_and_bare_id():
    from app.filters import load_sources

    keys = {item["chat_key"] for item in chat_rating.source_chats(load_sources())}
    assert "biznes_chat" in keys
    assert "id-1615175239" in keys


def test_rules_allow_card_on_monday_with_limits():
    text = (
        "Визитки по понедельникам с хэштегом #визитка. "
        "Одна ссылка. Не более 1 контакта. В личку не писать. "
        "Не чаще 1 раза в неделю. Формат: имя — услуга — контакт."
    )
    rules = chat_rating.extract_rules("", text)
    assert rules["cards_allowed"] is True
    assert rules["required_hashtag"] == "#визитка"
    assert rules["allowed_days"] == ["пн"]
    assert rules["link_limit"] == 1
    assert rules["contact_limit"] == 1
    assert rules["dm_banned"] is True
    assert "не чаще" in (rules["max_frequency"] or "")
    assert "имя" in (rules["required_format"] or "")
    assert rules["confident"] is True
    assert rules["needs_llm"] is False


def test_rules_forbid_ads_and_cards():
    rules = chat_rating.extract_rules("Без рекламы и без визиток. За рекламу бан.", "")
    assert rules["ads_allowed"] is False
    assert rules["cards_allowed"] is False
    assert rules["needs_llm"] is False


def test_rules_cards_allowed_even_if_ads_removed():
    rules = chat_rating.extract_rules(
        "Рекламные сообщения удаляются. Визитки по четвергам можно.",
        "",
    )
    assert rules["ads_allowed"] is False
    assert rules["cards_allowed"] is True
    assert rules["allowed_days"] == ["чт"]


def test_unclear_rules_skip_llm_without_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")

    async def fail_post(*args, **kwargs):
        raise AssertionError("модель не нужна без ключа")

    rules = chat_rating.extract_rules("По рекламе решайте с администратором чата.", "")
    assert rules["needs_llm"] is True

    async def run():
        return await chat_rating.clarify_rules(rules, "По рекламе решайте с администратором чата.", "", post=fail_post)

    merged = asyncio.run(run())
    assert merged["llm_used"] is False
    assert merged["cards_allowed"] is None


def test_clear_rules_do_not_call_llm_even_with_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    async def fail_post(*args, **kwargs):
        raise AssertionError("эвристика уже уверена")

    rules = chat_rating.extract_rules("Реклама запрещена.", "")

    async def run():
        return await chat_rating.clarify_rules(rules, "Реклама запрещена.", "", post=fail_post)

    merged = asyncio.run(run())
    assert merged["ads_allowed"] is False
    assert merged["llm_used"] is False


def test_llm_fills_only_unclear_rules(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    seen = {}

    async def post(url, payload, headers):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"ads_allowed": false, "cards_allowed": true, '
                            '"required_hashtag": "#визитка", "allowed_days": ["пн"], '
                            '"link_limit": 0, "dm_banned": true, "max_frequency": "раз в неделю"}'
                        )
                    }
                }
            ]
        }

    rules = chat_rating.extract_rules("Условия рекламы смотрите у админов.", "")

    async def run():
        return await chat_rating.clarify_rules(rules, "Условия рекламы смотрите у админов.", "", post=post)

    merged = asyncio.run(run())
    assert merged["llm_used"] is True
    assert merged["cards_allowed"] is True
    assert merged["ads_allowed"] is False
    assert merged["required_hashtag"] == "#визитка"
    assert merged["link_limit"] == 0
    assert merged["dm_banned"] is True
    assert seen["auth"] == "Bearer test-key"
    assert seen["url"].endswith("/chat/completions")


def test_minhash_marks_copies_and_not_unique_orders():
    copied = "Визитка. Сделаю сайт под ключ, пишите в личку, прайс в закрепе канала услуг."
    copies = [
        {"text": copied, "sender": "1", "bot": False, "date": NOW + timedelta(hours=index)}
        for index in range(30)
    ]
    spam = chat_rating.spam_metrics(copies)
    assert spam["duplicate_share"] > 0.8
    assert spam["repost_share"] > 0.8
    assert spam["ad_share"] == 1
    assert spam["spam_score"] >= chat_rating.SPAM_DROP_SCORE

    orders = [
        {
            "text": f"Ищу разработчика каталога для магазина номер {index}, бюджет {20000 + index} рублей",
            "sender": str(index),
            "bot": False,
            "date": NOW + timedelta(hours=index),
        }
        for index in range(30)
    ]
    quiet = chat_rating.spam_metrics(orders)
    assert quiet["ad_share"] == 0
    assert quiet["duplicate_share"] < 0.2
    assert quiet["spam_score"] < chat_rating.SPAM_DROP_SCORE


def test_bot_share_and_messages_per_day():
    messages = [
        {"text": f"заявка {index}", "sender": "bot", "bot": True, "date": NOW}
        for index in range(4)
    ]
    messages.append(
        {
            "text": "живой вопрос про каталог",
            "sender": "human",
            "bot": False,
            "date": NOW + timedelta(days=4),
        }
    )
    spam = chat_rating.spam_metrics(messages)
    assert spam["bot_share"] == pytest.approx(0.8)
    assert spam["messages_per_day"] == pytest.approx(5 / 4)


def test_status_matrix():
    quiet = {"messages_collected": 40, "spam_score": 0.1}
    loud = {"messages_collected": 40, "spam_score": 0.9}
    allowed = {"cards_allowed": True, "ads_allowed": None, "needs_llm": False}
    forbidden = {"cards_allowed": False, "ads_allowed": False, "needs_llm": False}
    unclear = {"cards_allowed": None, "ads_allowed": None, "needs_llm": True}
    leads = {"fit3": 2, "rejected": 1}

    status, reason = chat_rating.decide_status(
        forbidden, loud, member=True, can_write=True, leads=leads
    )
    assert status == "no_ads"
    assert "запрещены" in reason
    assert "2" in reason and "1" in reason

    status, reason = chat_rating.decide_status(
        allowed, loud, member=True, can_write=True
    )
    assert status == "spam_drop"
    assert "потеряется" in reason

    status, reason = chat_rating.decide_status(
        allowed,
        quiet,
        member=True,
        can_write=True,
        leads=leads,
    )
    assert status == "active"
    assert "писать можно" in reason

    status, reason = chat_rating.decide_status(
        allowed, quiet, member=False, can_write=False
    )
    assert status == "unknown"
    assert "не состоим" in reason

    status, reason = chat_rating.decide_status(
        unclear, quiet, member=True, can_write=True
    )
    assert status == "unknown"

    status, reason = chat_rating.decide_status(
        allowed,
        quiet,
        member=True,
        can_write=False,
        write_note="это канал, писать могут только админы",
    )
    assert status == "unknown"
    assert "канал" in reason

    rating = chat_rating.compute_rating(
        allowed, quiet, leads, member=True, can_write=True
    )
    low = chat_rating.compute_rating(
        forbidden, loud, {"fit3": 0, "rejected": 4}, member=False, can_write=False
    )
    assert rating > low


def test_schedule_spreads_active_chats_over_ten_days():
    keys = ["d", "a", "c", "b"]
    slots = chat_rating.plan_slots(keys, NOW)
    assert set(slots) == set(keys)
    moments = sorted(slots.values())
    assert all(moment > NOW for moment in moments)
    assert moments[-1] - moments[0] >= timedelta(days=7)
    assert moments[-1] - NOW <= timedelta(days=10)
    deltas = [(moments[index + 1] - moments[index]).total_seconds() for index in range(3)]
    assert max(deltas) - min(deltas) < 2
    again = chat_rating.plan_slots(keys, NOW)
    assert again == slots
    assert chat_rating.plan_slots([], NOW) == {}


def test_draft_obeys_hashtag_link_ban_and_dm_ban():
    rules = {
        "cards_allowed": True,
        "ads_allowed": False,
        "required_hashtag": "#визитка",
        "required_format": None,
        "allowed_days": ["пн"],
        "link_limit": 0,
        "contact_limit": None,
        "max_frequency": "не чаще 1 раза в неделю",
        "dm_banned": True,
    }
    draft = chat_rating.draft_card("Чат селлеров Wildberries", rules)
    text = draft["text"]
    assert "технический партнёр для бизнеса" in text
    assert "#визитка" in text
    assert "http" not in text.lower()
    assert "@pasha_kvk" not in text
    assert "в личку" not in text.lower()
    assert "пн" in draft["note"]
    assert "Wildberries" in text or "вайлдберриз" in text.lower() or "селлер" in text.lower()

    open_rules = {
        "cards_allowed": True,
        "ads_allowed": True,
        "required_hashtag": None,
        "link_limit": None,
        "contact_limit": None,
        "dm_banned": False,
        "allowed_days": [],
    }
    first = chat_rating.draft_card("n8n сообщество", open_rules)["text"]
    second = chat_rating.draft_card("МойСклад", open_rules)["text"]
    assert "pavelklyushin.ru" in first
    assert "@pasha_kvk" in first
    assert first != second

    refused = chat_rating.draft_card("Закрытый клуб", {"cards_allowed": False, "ads_allowed": False})
    assert refused["text"] == ""
    assert "запрещена" in refused["note"]


def test_write_permission_for_group_channel_and_ban():
    group = SimpleNamespace(broadcast=False, megagroup=True, default_banned_rights=None)
    member = SimpleNamespace(is_banned=False, has_left=False, is_admin=False, is_creator=False)
    assert chat_rating.write_permission(group, member)[:2] == (True, True)

    channel = SimpleNamespace(broadcast=True, megagroup=False)
    assert chat_rating.write_permission(channel, member)[1] is False
    admin = SimpleNamespace(is_banned=False, has_left=False, is_admin=True, is_creator=False)
    assert chat_rating.write_permission(channel, admin)[1] is True

    banned = SimpleNamespace(
        is_banned=True,
        has_left=False,
        participant=SimpleNamespace(banned_rights=SimpleNamespace(send_messages=True)),
    )
    assert chat_rating.write_permission(group, banned)[1] is False
    assert chat_rating.write_permission(group, None)[0] is False


def test_scan_reads_pinned_verbatim_and_waits_flood():
    entity = SimpleNamespace(
        kind="channel",
        id=42,
        title="Магазин чат",
        username="shop",
        broadcast=False,
        megagroup=True,
        default_banned_rights=None,
    )

    class Message:
        def __init__(self, mid, text):
            self.id = mid
            self.message = text
            self.sender_id = 9
            self.sender = None
            self.via_bot_id = None
            self.date = NOW

    class Client:
        def __init__(self):
            self.calls = 0
            self.joined = False

        async def get_entity(self, name):
            self.calls += 1
            if self.calls == 1:
                raise FloodWaitError(None, capture=1)
            return entity

        async def __call__(self, request):
            if "Join" in type(request).__name__ or "Import" in type(request).__name__:
                self.joined = True
                raise AssertionError(type(request).__name__)
            return SimpleNamespace(
                full_chat=SimpleNamespace(about="Описание как есть\nЁлка", pinned_msg_id=7)
            )

        async def get_messages(self, entity, ids=None, limit=None, filter=None):
            if ids:
                return [Message(7, "Закреп дословно")]
            return []

        async def get_permissions(self, entity, me):
            return SimpleNamespace(is_banned=False, has_left=False, is_admin=False, is_creator=False)

        def iter_messages(self, entity, limit=500):
            async def generate():
                yield Message(1, "Ищу каталог для интернет-магазина, бюджет 80000")

            return generate()

    waits: list[int] = []

    async def sleep(seconds):
        waits.append(seconds)

    client = Client()

    async def run():
        return await chat_scan.scan_chat(
            client,
            {"chat_key": "shop", "username": "shop", "channel_id": None, "title": "Магазин", "join": ""},
            SimpleNamespace(id=1),
            sleep=sleep,
        )

    raw = asyncio.run(run())
    assert raw["pinned_text"] == "Закреп дословно"
    assert raw["description"] == "Описание как есть\nЁлка"
    assert raw["member"] is True
    assert raw["can_write"] is True
    assert waits == [2]
    assert client.joined is False


def test_scan_does_not_join_closed_chat():
    class Client:
        async def get_entity(self, name):
            raise ChannelPrivateError(None)

        async def __call__(self, request):
            raise AssertionError(type(request).__name__)

    async def run():
        await chat_scan.scan_chat(
            Client(),
            {"chat_key": "closed", "username": "closed", "title": "Закрыт", "join": "candidate"},
            SimpleNamespace(id=1),
            sleep=_no_sleep,
        )

    with pytest.raises(RuntimeError, match="не вступает"):
        asyncio.run(run())


def test_not_a_member_is_recorded():
    entity = SimpleNamespace(
        kind="channel",
        id=3,
        title="Чужой",
        username="foreign",
        broadcast=False,
        megagroup=True,
        default_banned_rights=None,
    )

    class Client:
        async def get_entity(self, name):
            return entity

        async def __call__(self, request):
            return SimpleNamespace(full_chat=SimpleNamespace(about="Визитки можно", pinned_msg_id=None))

        async def get_messages(self, entity, ids=None, limit=None, filter=None):
            return []

        async def get_permissions(self, entity, me):
            raise UserNotParticipantError(None)

        def iter_messages(self, entity, limit=500):
            raise AssertionError("историю не читаем, если нас нет в чате")

    async def run():
        return await chat_scan.scan_chat(
            Client(),
            {"chat_key": "foreign", "username": "foreign", "title": "Чужой", "join": ""},
            SimpleNamespace(id=1),
            sleep=_no_sleep,
        )

    raw = asyncio.run(run())
    assert raw["member"] is False
    assert raw["can_write"] is False
    assert raw["messages"] == []


def test_reader_module_has_no_send_or_join():
    text = (ROOT / "app" / "chat_scan.py").read_text(encoding="utf-8")
    for banned in ("JoinChannelRequest", "ImportChatInviteRequest", "send_message", "SendMessageRequest"):
        assert banned not in text


def test_tgstat_skips_without_token_and_parses_with_token(monkeypatch):
    monkeypatch.setattr(settings, "tgstat_token", "")
    called = {"n": 0}

    async def fetch(url, params):
        called["n"] += 1
        return 500, ""

    assert asyncio.run(tgstat.enrich("shop", fetch=fetch)) is None
    assert called["n"] == 0

    monkeypatch.setattr(settings, "tgstat_token", "secret")

    async def api(url, params):
        assert params["token"] == "secret"
        assert params["channelId"] == "@shop"
        body = (
            '{"status":"ok","response":{"participants_count":1200,"title":"Магазин",'
            '"about":"про каталог","peer_type":"chat","ci_index":4.5}}'
        )
        return 200, body

    payload = asyncio.run(tgstat.enrich("shop", fetch=api))
    assert payload["ok"] is True
    assert payload["participants"] == 1200
    assert payload["source"] == "api"

    async def broken(url, params):
        if "api.tgstat" in url:
            return 200, '{"status":"error","error":"channel_not_found"}'
        return 200, "<div>1 200 участников</div>"

    page = asyncio.run(tgstat.enrich("shop", fetch=broken))
    assert page["source"] == "page"
    assert page["participants"] == 1200

    async def down(url, params):
        raise TimeoutError("нет сети")

    failed = asyncio.run(tgstat.enrich("shop", fetch=down))
    assert failed["ok"] is False


def test_profile_keeps_verbatim_text_override_and_lead_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")

    async def scenario():
        conn = await db.connect()
        try:
            await conn.execute(
                """
                INSERT INTO leads (
                    source, channel_username, channel_id, message_id, text, status, fit, created_at
                ) VALUES
                    ('telegram', 'Shop', '42', 1, 'хороший заказ', 'new', 4, '2026-10-01'),
                    ('telegram', 'shop', '42', 2, 'отказ', 'reject', 5, '2026-10-02'),
                    ('telegram', 'other', '9', 3, 'чужой', 'new', 5, '2026-10-03')
                """
            )
            await conn.commit()
            leads = await db.lead_counts_for_chat(conn, "shop", "42")
            assert leads == {"fit3": 2, "rejected": 1}

            description = "Описание как есть\nЁлка"
            pinned = "Закреп дословно"
            rules = chat_rating.extract_rules(description, "Визитки можно. " + pinned)
            raw = {
                "chat_key": "shop",
                "username": "shop",
                "channel_id": 42,
                "title": "Магазин",
                "description": description,
                "pinned_text": pinned,
                "member": True,
                "can_write": True,
                "write_note": "",
                "join": "",
                "read_error": "",
                "messages": [],
            }
            record = chat_scan.assemble_record(raw, rules, chat_rating.spam_metrics([]), leads, None)
            await db.upsert_chat_observation(conn, record)
            await db.set_chat_override(conn, "shop", "no_ads")
            record["description"] = description
            record["rating"] = 11
            await db.upsert_chat_observation(conn, record)
            stored = await db.get_chat_profile(conn, "shop")
            assert stored["description"] == description
            assert stored["pinned_text"] == pinned
            assert stored["status_override"] == "no_ads"
            assert stored["leads_fit3"] == 2
            assert stored["leads_rejected"] == 1

            weak = dict(record)
            weak["chat_key"] = "quiet"
            weak["username"] = "quiet"
            weak["title"] = "Тихий"
            weak["rules"] = chat_rating.extract_rules("Просто общаемся.", "")
            weak.update(chat_rating.spam_metrics([]))
            status, reason = chat_rating.decide_status(
                weak["rules"],
                chat_rating.spam_metrics([]),
                member=True,
                can_write=True,
            )
            weak["status_auto"] = status
            weak["status_reason"] = reason
            weak["rating"] = 20
            await db.upsert_chat_observation(conn, weak)
            await db.set_chat_override(conn, "shop", None)
            await chat_rating.refresh_card_plan(conn, NOW)
            rows = {row["chat_key"]: row for row in await db.list_chat_profiles(conn)}
            assert rows["shop"]["next_card_at"]
            assert rows["quiet"]["next_card_at"] is None
            assert "технический партнёр" in rows["shop"]["card_draft"]
        finally:
            await conn.close()

    asyncio.run(scenario())


def test_chats_page_sorts_and_accepts_manual_status(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "page.db")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    async def prepare():
        conn = await db.connect()
        try:
            for key, title, rating, status in (
                ("alpha", "Альфа", 10, "unknown"),
                ("yacht", "Яхта", 90, "active"),
            ):
                rules = {"cards_allowed": status == "active", "ads_allowed": None, "needs_llm": False,
                         "allowed_days": [], "dm_banned": None, "confident": True, "conflict": False, "llm_used": False}
                raw = {
                    "chat_key": key,
                    "username": key,
                    "channel_id": "",
                    "title": title,
                    "description": "Визитки можно" if status == "active" else "Просто чат",
                    "pinned_text": "дословно",
                    "member": True,
                    "can_write": True,
                    "write_note": "",
                    "join": "",
                    "read_error": "",
                }
                record = chat_scan.assemble_record(
                    raw, rules, chat_rating.spam_metrics([]), {"fit3": 0, "rejected": 0}, None
                )
                record["rating"] = rating
                record["status_auto"] = status
                await db.upsert_chat_observation(conn, record)
            await chat_rating.refresh_card_plan(conn, NOW)
        finally:
            await conn.close()

    asyncio.run(prepare())

    from fastapi.testclient import TestClient

    from app import boards, collector, networking, notifier
    from app.main import app

    async def quiet():
        return None

    async def signed_out():
        return False

    monkeypatch.setattr(boards, "poll_forever", quiet)
    monkeypatch.setattr(networking, "poll_forever", quiet)
    monkeypatch.setattr(notifier, "start", quiet)
    monkeypatch.setattr(notifier, "stop", quiet)
    monkeypatch.setattr(collector, "is_authorized", signed_out)

    with TestClient(app) as client:
        page = client.get("/chats")
        assert page.status_code == 200
        body = page.text
        assert "Чаты" in body
        assert body.index("Яхта") < body.index("Альфа")
        assert "дословно" in body
        assert "можно визитку" in body
        low_first = client.get("/chats?sort=rating&dir=asc")
        assert low_first.text.index("Альфа") < low_first.text.index("Яхта")
        saved = client.post(
            "/chats/alpha/status",
            data={"status": "active", "next": "/chats?sort=rating&dir=asc"},
            follow_redirects=True,
        )
        assert saved.status_code == 200
        assert "Статус поставлен вручную" in saved.text
        draft = client.post(
            "/chats/yacht/draft",
            data={"next": "/chats"},
            follow_redirects=True,
        )
        assert "технический партнёр для бизнеса" in draft.text
        health = client.get("/health")
        assert health.json()["chat_rate"] is True


def test_internal_rate_route_is_local(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "rate.db")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    chat_scan.JOB.update(running=False, finished_at=None, summary=None, error="")

    async def fake_scan(**kwargs):
        chat_scan.JOB.update(
            running=False,
            done=1,
            total=1,
            finished_at="2026-10-11T00:00:00+00:00",
            summary={"saved": 1, "skipped": 0, "errors": [], "stopped": "", "total": 1},
            error="",
        )

    monkeypatch.setattr(chat_scan, "run_scan", fake_scan)

    from fastapi.testclient import TestClient

    from app import boards, collector, networking, notifier
    from app.main import app

    async def quiet():
        return None

    async def signed_out():
        return False

    monkeypatch.setattr(boards, "poll_forever", quiet)
    monkeypatch.setattr(networking, "poll_forever", quiet)
    monkeypatch.setattr(notifier, "start", quiet)
    monkeypatch.setattr(notifier, "stop", quiet)
    monkeypatch.setattr(collector, "is_authorized", signed_out)

    with TestClient(app) as client:
        started = client.post("/internal/chat-rate", params={"limit": 1})
        assert started.status_code == 200
        assert started.json()["started"] is True
        status = client.get("/internal/chat-rate")
        assert status.json()["ok"] is True


def test_plan_run_never_opens_a_second_session_while_inbox_is_up():
    assert plan_run(inbox_up=True, can_delegate=True, session_busy=True) == "delegate"
    assert plan_run(inbox_up=True, can_delegate=False, session_busy=False) == "busy"
    assert plan_run(inbox_up=False, can_delegate=False, session_busy=True) == "busy"
    assert plan_run(inbox_up=False, can_delegate=False, session_busy=False) == "local"


def test_session_lock_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_path", tmp_path / "telegram")
    release()
    ensure("rate")
    try:
        import os
        import subprocess
        import sys

        code = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ['APP_ROOT'])\n"
            "from app.session_gate import SessionBusy, ensure\n"
            "try:\n"
            "    ensure('other')\n"
            "    print('free')\n"
            "except SessionBusy:\n"
            "    print('busy')\n"
        )
        env = os.environ.copy()
        env["APP_ROOT"] = str(ROOT)
        env["SESSION_PATH"] = str(tmp_path / "telegram")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.stdout.strip() == "busy", completed.stderr
    finally:
        release()


def test_session_sqlite_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_path", tmp_path / "telegram")
    path = tmp_path / "telegram.session"
    holder = sqlite3.connect(path)
    holder.execute("CREATE TABLE t(id INTEGER)")
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert session_sqlite_busy() is True
    finally:
        holder.rollback()
        holder.close()
    assert session_sqlite_busy() is False


def test_script_does_not_open_session_when_inbox_is_up(monkeypatch):
    module = _script()
    monkeypatch.setattr(module, "probe_inbox", lambda: {"ok": True, "authorized": True})

    def forbidden(*args, **kwargs):
        raise AssertionError("сессию трогать нельзя")

    monkeypatch.setattr(module, "ensure", forbidden)
    monkeypatch.setattr(module, "session_sqlite_busy", forbidden)
    assert module.main([]) == 3


def test_script_delegates_to_running_inbox(monkeypatch):
    module = _script()
    monkeypatch.setattr(module, "probe_inbox", lambda: {"ok": True, "chat_rate": True, "authorized": True})
    monkeypatch.setattr(module, "ensure", lambda role: (_ for _ in ()).throw(AssertionError("lock")))

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    calls = {"post": 0}

    class FakeHttp:
        def post(self, url, params=None, timeout=None):
            calls["post"] += 1
            assert "/internal/chat-rate" in url
            return Response({"ok": True, "started": True})

        def get(self, url, timeout=None):
            return Response(
                {
                    "ok": True,
                    "running": False,
                    "done": 0,
                    "total": 0,
                    "current": "",
                    "error": "",
                    "finished_at": "2026-10-11T00:00:00+00:00",
                    "summary": {"saved": 0, "skipped": 0, "errors": [], "stopped": ""},
                }
            )

    monkeypatch.setattr(module, "httpx", FakeHttp())
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    async def rows():
        return []

    monkeypatch.setattr(module, "_load_rows", rows)
    assert module.main([]) == 0
    assert calls["post"] == 1


def _script():
    path = ROOT / "scripts" / "rate_chats.py"
    spec = importlib.util.spec_from_file_location("rate_chats_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _no_sleep(seconds):
    return None
