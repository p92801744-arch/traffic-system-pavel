"""Только чтение чатов из sources.yaml. Вступать и писать этот модуль не умеет."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import InputMessagesFilterPinned, PeerChannel

from app import chat_rating, db, tgstat
from app.collector import make_client, work_lock
from app.filters import load_sources
log = logging.getLogger("leads.chat_scan")

FLOOD_BUDGET_SECONDS = 1800
FRESH_HOURS = 12

JOB: dict = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": "",
    "error": "",
    "finished_at": None,
    "summary": None,
}


class FloodBudgetExceeded(RuntimeError):
    def __init__(self, label: str, seconds: int):
        self.label = label
        self.seconds = seconds
        super().__init__(f"{label}: Telegram просит ждать {seconds}с, это больше лимита прогона")


async def respect_flood(factory, *, sleep=asyncio.sleep, label: str = "", max_wait: int = FLOOD_BUDGET_SECONDS):
    """Повторяет вызов после FloodWait. Слишком долгую паузу не держит процесс занятым."""
    while True:
        try:
            return await factory()
        except FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            wait = max(seconds, 0) + 1
            if wait > max_wait:
                raise FloodBudgetExceeded(label, wait) from exc
            log.warning("FloodWait %sс на «%s», жду и не открываю вторую сессию", wait, label or "чат")
            await sleep(wait)


async def run_guarded(
    *,
    only: str = "",
    limit: int = 0,
    force: bool = False,
    max_messages: int = 500,
    pause: float = 1.5,
    sleep=asyncio.sleep,
) -> dict:
    """Просит слушателя супервизора отдать сессию. Второй клиент не открывает.

    Пока лежит data/JOIN_IN_PROGRESS, Telegram не трогает.
    Клиент закрывается до того, как замок вернётся слушателю.
    """
    from app.collector import disconnect, join_lock_active
    from app.servicekit import runtime_dir
    from app.session_gate import borrow_session

    if join_lock_active():
        raise RuntimeError("Идёт вступление в чаты, сессию не открываю")
    async with borrow_session(runtime_dir(), timeout=120):
        try:
            return await run_scan(
                only=only,
                limit=limit,
                force=force,
                max_messages=max_messages,
                pause=pause,
                sleep=sleep,
            )
        finally:
            await disconnect()


async def run_scan(
    *,
    only: str = "",
    limit: int = 0,
    force: bool = False,
    max_messages: int = 500,
    pause: float = 1.5,
    sleep=asyncio.sleep,
) -> dict:
    async with work_lock:
        return await _run_scan(
            only=only,
            limit=limit,
            force=force,
            max_messages=max_messages,
            pause=pause,
            sleep=sleep,
        )


async def _run_scan(*, only: str, limit: int, force: bool, max_messages: int, pause: float, sleep) -> dict:
    chats = chat_rating.source_chats(load_sources())
    needle = only.lstrip("@").strip().lower()
    if needle:
        chats = [chat for chat in chats if chat["chat_key"] == needle or (chat.get("username") or "").lower() == needle]
    if limit > 0:
        chats = chats[:limit]

    JOB.update(
        running=True,
        done=0,
        total=len(chats),
        current="",
        error="",
        finished_at=None,
        summary=None,
    )
    saved = 0
    skipped = 0
    errors: list[str] = []
    stopped = ""
    conn = None
    try:
        conn = await db.connect()
        client = make_client()
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram не авторизован")
        me = await client.get_me()
        fresh = await db.fresh_chat_keys(conn, hours=FRESH_HOURS)
        for chat in chats:
            JOB["current"] = chat["chat_key"]
            if not force and chat["chat_key"] in fresh:
                skipped += 1
                JOB["done"] += 1
                continue
            try:
                raw = await scan_chat(client, chat, me, max_messages=max_messages, sleep=sleep)
                rules = chat_rating.extract_rules(raw["description"], raw["pinned_text"])
                rules = await chat_rating.clarify_rules(rules, raw["description"], raw["pinned_text"])
                spam = chat_rating.spam_metrics(raw["messages"])
                leads = await db.lead_counts_for_chat(conn, raw.get("username"), raw.get("channel_id"))
                tg = await tgstat.enrich(raw.get("username"))
                record = assemble_record(raw, rules, spam, leads, tg)
                await db.upsert_chat_observation(conn, record)
                saved += 1
            except FloodBudgetExceeded as exc:
                stopped = str(exc)
                errors.append(stopped)
                log.warning("%s", exc)
                break
            except Exception as exc:
                errors.append(f"{chat['chat_key']}: {exc}")
                log.warning("Чат %s не прочитан: %s", chat["chat_key"], exc)
                await db.upsert_chat_observation(
                    conn,
                    assemble_record(
                        {
                            **chat,
                            "description": "",
                            "pinned_text": "",
                            "member": None,
                            "can_write": None,
                            "write_note": "",
                            "messages": [],
                            "read_error": f"Не удалось прочитать чат: {exc}",
                        },
                        chat_rating.extract_rules("", ""),
                        chat_rating.spam_metrics([]),
                        {"fit3": 0, "rejected": 0},
                        None,
                    ),
                )
            JOB["done"] += 1
            if pause:
                await sleep(pause)
        await chat_rating.refresh_card_plan(conn, datetime.now(timezone.utc))
    finally:
        if conn is not None:
            await conn.close()
        JOB["running"] = False
        JOB["current"] = ""
        JOB["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    summary = {
        "saved": saved,
        "skipped": skipped,
        "errors": errors,
        "stopped": stopped,
        "total": len(chats),
    }
    JOB["summary"] = summary
    JOB["error"] = stopped
    return summary


def assemble_record(raw: dict, rules: dict, spam: dict, leads: dict, tgstat_payload: dict | None) -> dict:
    member = raw.get("member")
    can_write = raw.get("can_write")
    rating = chat_rating.compute_rating(
        rules,
        spam,
        leads,
        member=member,
        can_write=can_write,
    )
    status, reason = chat_rating.decide_status(
        rules,
        spam,
        member=member,
        can_write=can_write,
        read_error=raw.get("read_error") or "",
        description=raw.get("description") or "",
        pinned=raw.get("pinned_text") or "",
        leads=leads,
        write_note=raw.get("write_note") or "",
    )
    draft = chat_rating.draft_card(raw.get("title") or "", rules)
    return {
        "chat_key": raw["chat_key"],
        "username": raw.get("username") or "",
        "channel_id": "" if raw.get("channel_id") in (None, "") else str(raw.get("channel_id")),
        "title": raw.get("title") or raw["chat_key"],
        "description": raw.get("description") or "",
        "pinned_text": raw.get("pinned_text") or "",
        "member": member,
        "can_write": can_write,
        "write_note": raw.get("write_note") or "",
        "read_error": raw.get("read_error") or "",
        "listed_join": raw.get("join") or "",
        "rating": rating,
        "status_auto": status,
        "status_reason": reason,
        "card_draft": draft["text"],
        "card_draft_note": draft["note"],
        "tgstat_json": tgstat_payload,
        "leads_fit3": int(leads.get("fit3") or 0),
        "leads_rejected": int(leads.get("rejected") or 0),
        **spam,
        "rules": rules,
    }


async def scan_chat(client, chat: dict, me, *, max_messages: int = 500, sleep=asyncio.sleep) -> dict:
    label = chat.get("username") or chat["chat_key"]
    entity = await respect_flood(lambda: _resolve(client, chat), sleep=sleep, label=label)
    kind = _kind(entity)
    if kind == "user":
        raise RuntimeError("это пользователь, не чат")
    about, pinned_ids = await respect_flood(
        lambda: _about(client, entity, kind),
        sleep=sleep,
        label=label,
    )
    pinned_text = await _pinned_text(client, entity, pinned_ids, sleep=sleep, label=label)
    member, can_write, write_note = await _membership(client, entity, me)
    messages: list[dict] = []
    read_error = ""
    if member is False:
        read_error = ""
    else:
        try:
            messages = await _messages(client, entity, max_messages, sleep=sleep, label=label)
        except FloodBudgetExceeded:
            raise
        except Exception as exc:
            read_error = f"Историю сообщений прочитать не удалось: {exc}"
    return {
        **chat,
        "title": getattr(entity, "title", None) or chat.get("title") or label,
        "username": getattr(entity, "username", None) or chat.get("username") or "",
        "channel_id": getattr(entity, "id", None) or chat.get("channel_id"),
        "description": about,
        "pinned_text": pinned_text,
        "member": member,
        "can_write": can_write,
        "write_note": write_note,
        "messages": messages,
        "read_error": read_error,
    }


async def _resolve(client, chat: dict):
    username = (chat.get("username") or "").lstrip("@")
    if username:
        try:
            return await client.get_entity(username)
        except (UsernameNotOccupiedError, UsernameInvalidError) as exc:
            raise RuntimeError("такого чата нет") from exc
        except ChannelPrivateError as exc:
            raise RuntimeError("чат закрыт, скрипт сам не вступает") from exc
        except ValueError as exc:
            raise RuntimeError("чат не найден в сессии") from exc
    channel_id = chat.get("channel_id")
    if channel_id:
        try:
            return await client.get_entity(PeerChannel(int(channel_id)))
        except ChannelPrivateError as exc:
            raise RuntimeError("чат закрыт, скрипт сам не вступает") from exc
        except ValueError as exc:
            raise RuntimeError("чат не найден в сессии") from exc
    raise RuntimeError("у источника нет username и id, инвайт скрипт не открывает")


def _kind(entity) -> str:
    hinted = getattr(entity, "kind", None)
    if hinted in {"channel", "chat", "user"}:
        return hinted
    name = type(entity).__name__
    if name == "User":
        return "user"
    if name == "Chat":
        return "chat"
    return "channel"


async def _about(client, entity, kind: str) -> tuple[str, list[int]]:
    if kind == "chat":
        full = await client(GetFullChatRequest(entity.id))
    else:
        full = await client(GetFullChannelRequest(entity))
    chat = getattr(full, "full_chat", None)
    about = getattr(chat, "about", None) or ""
    pinned = getattr(chat, "pinned_msg_id", None)
    return about, [int(pinned)] if pinned else []


async def _pinned_text(client, entity, pinned_ids: list[int], *, sleep, label: str) -> str:
    texts: list[str] = []
    seen: set[int] = set()

    async def load_ids():
        if not pinned_ids:
            return []
        got = await client.get_messages(entity, ids=pinned_ids)
        if got is None:
            return []
        return got if isinstance(got, list) else [got]

    try:
        for message in await respect_flood(load_ids, sleep=sleep, label=label):
            if message is None:
                continue
            seen.add(int(getattr(message, "id", 0) or 0))
            text = getattr(message, "message", None) or ""
            if text:
                texts.append(text)
    except FloodBudgetExceeded:
        raise
    except Exception:
        log.info("Закреп %s по id не прочитался", label)

    async def load_filter():
        return await client.get_messages(entity, limit=10, filter=InputMessagesFilterPinned())

    try:
        extra = await respect_flood(load_filter, sleep=sleep, label=label)
    except FloodBudgetExceeded:
        raise
    except Exception:
        extra = []
    if extra is None:
        extra = []
    if not isinstance(extra, list):
        extra = [extra]
    for message in extra:
        if message is None:
            continue
        mid = int(getattr(message, "id", 0) or 0)
        if mid in seen:
            continue
        text = getattr(message, "message", None) or ""
        if text:
            texts.append(text)
    return "\n\n---\n\n".join(texts)


async def _membership(client, entity, me) -> tuple[bool | None, bool | None, str]:
    try:
        perms = await client.get_permissions(entity, me)
    except UserNotParticipantError:
        return False, False, "не состоим в чате"
    except Exception as exc:
        return None, None, f"права не прочитались: {exc}"
    return chat_rating.write_permission(entity, perms)


async def _messages(client, entity, limit: int, *, sleep, label: str) -> list[dict]:
    async def collect():
        rows: list[dict] = []
        async for message in client.iter_messages(entity, limit=limit):
            sender = getattr(message, "sender", None)
            username = getattr(sender, "username", None) if sender else None
            bot = bool(getattr(sender, "bot", False)) or bool(getattr(message, "via_bot_id", None))
            if username and str(username).lower().endswith("bot"):
                bot = True
            rows.append(
                {
                    "text": getattr(message, "message", None) or "",
                    "sender": str(getattr(message, "sender_id", None) or username or ""),
                    "bot": bot,
                    "date": getattr(message, "date", None),
                }
            )
        return rows

    return await respect_flood(collect, sleep=sleep, label=label)
