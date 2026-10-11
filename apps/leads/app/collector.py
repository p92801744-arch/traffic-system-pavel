from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready

from app import db
from app.contacts import extract_contact
from app.filters import intake_hits, load_sources
from app.notifier import send_lead
from app.scorer import score_lead
from app.settings import settings

log = logging.getLogger("leads.collector")

_client: TelegramClient | None = None
_listener_started = False
_watch_ids: set[int] = set()


def make_client() -> TelegramClient:
    global _client
    if _client is None:
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        _client = TelegramClient(
            str(settings.session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
    return _client


async def is_authorized() -> bool:
    if not settings.has_telegram_api:
        return False
    client = make_client()
    if not client.is_connected():
        await client.connect()
    return await client.is_user_authorized()


async def send_login_code(phone: str) -> str:
    client = make_client()
    if not client.is_connected():
        await client.connect()
    result = await client.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(phone: str, code: str, phone_code_hash: str, password: str | None = None):
    client = make_client()
    if not client.is_connected():
        await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise
        await client.sign_in(password=password)


def watched_channels(sources: dict) -> list[dict]:
    """Чаты для входа и чтения. Кандидаты остаются Павлу: сам вступает со своего аккаунта."""
    found: list[dict] = []
    for channel in sources.get("channels") or []:
        if str(channel.get("join") or "").strip().lower() == "candidate":
            continue
        found.append(channel)
    return found


def _source_spec(channel: dict) -> tuple[str | None, str | None, str]:
    title = (channel.get("title") or "").strip()
    username = (channel.get("username") or "").lstrip("@") or None
    invite = (channel.get("invite") or "").strip() or None
    url = (channel.get("url") or "").strip()
    if url:
        url = url.split("?")[0].rstrip("/")
        if "/+" in url:
            invite = url.split("+", 1)[1]
            username = None
        elif "joinchat/" in url.lower():
            invite = url.rsplit("/", 1)[-1]
            username = None
        else:
            username = url.rsplit("/", 1)[-1].lstrip("@")
    return username, invite, title or username or "чат"


def _watch(entity) -> None:
    peer_id = utils.get_peer_id(entity)
    _watch_ids.add(int(peer_id))
    raw_id = getattr(entity, "id", None)
    if raw_id is not None:
        _watch_ids.add(int(raw_id))


def _in_watch(event) -> bool:
    if event.chat_id in _watch_ids:
        return True
    chat = getattr(event, "chat", None)
    return bool(chat) and int(getattr(chat, "id", 0) or 0) in _watch_ids


async def resolve_source(client: TelegramClient, channel: dict):
    username, invite, title = _source_spec(channel)
    raw_id = channel.get("channel_id")
    if raw_id and not username and not invite:
        from telethon.tl.types import PeerChannel

        entity = await client.get_entity(PeerChannel(int(raw_id)))
        try:
            await client(JoinChannelRequest(entity))
        except Exception:
            pass
        return entity, title or getattr(entity, "title", None) or str(raw_id)
    if invite:
        try:
            preview = await client(CheckChatInviteRequest(invite))
            if isinstance(preview, ChatInviteAlready):
                return preview.chat, title or getattr(preview.chat, "title", None)
            updates = await client(ImportChatInviteRequest(invite))
            chats = getattr(updates, "chats", None) or []
            if not chats:
                raise RuntimeError("инвайт принят, но чат не вернулся")
            return chats[0], title or getattr(chats[0], "title", None)
        except UserAlreadyParticipantError:
            preview = await client(CheckChatInviteRequest(invite))
            chat = getattr(preview, "chat", None)
            if chat is None:
                raise
            return chat, title or getattr(chat, "title", None)
        except (InviteHashExpiredError, InviteHashInvalidError) as exc:
            raise RuntimeError(f"инвайт недействителен: {exc}") from exc
    if not username:
        raise ValueError("у источника нет username и invite")
    entity = await client.get_entity(username)
    try:
        await client(JoinChannelRequest(entity))
    except Exception:
        pass
    return entity, title or getattr(entity, "title", None) or username


def _message_url(username: str | None, channel_id, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(channel_id).replace("-100", "")
    return f"https://t.me/c/{cid}/{message_id}"


async def _ingest_message(
    message,
    channel_username: str | None,
    channel_title: str | None,
    sources: dict,
    *,
    notify: bool = False,
) -> bool:
    text = (message.message or "").strip()
    if len(text) < 40:
        return False

    hits = intake_hits(text, sources)
    if not hits:
        return False

    chat = await message.get_chat()
    username = getattr(chat, "username", None) or channel_username
    title = getattr(chat, "title", None) or channel_title or username
    channel_id = getattr(chat, "id", None)
    date = message.date.astimezone(timezone.utc).isoformat(timespec="seconds") if message.date else None

    contact = await extract_contact(message, username)

    conn = await db.connect()
    try:
        lead_id = await db.upsert_lead(
            conn,
            {
                "source": "telegram",
                "channel_username": username,
                "channel_title": title,
                "channel_id": channel_id,
                "message_id": message.id,
                "message_date": date,
                "text": text,
                "url": _message_url(username, channel_id, message.id),
                "keywords": hits,
                **{k: contact[k] for k in ("sender_username", "sender_id", "sender_name", "write_url", "kind")},
            },
        )
        if not lead_id:
            return False
        score = await score_lead(text, username)
        await db.save_score(conn, lead_id, score)
        if notify:
            lead = await db.get_lead(conn, lead_id)
            if lead:
                await send_lead(lead)
        return True
    finally:
        await conn.close()


async def refresh_watch_list() -> dict:
    sources = load_sources()
    client = make_client()
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")

    joined = 0
    errors: list[str] = []
    for channel in watched_channels(sources):
        label = channel.get("username") or channel.get("invite") or channel.get("title") or "?"
        try:
            entity, _title = await resolve_source(client, channel)
            _watch(entity)
            joined += 1
            await asyncio.sleep(0.4)
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            errors.append(f"{label}: Telegram просит подождать {wait}с")
            log.warning("Telegram ограничил вход в чаты (~%sс). Дальше не долблю.", wait)
            break
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            log.warning("Источник %s недоступен: %s", label, exc)
    log.info("Слушаю %s чатов", len(_watch_ids))
    return {"watching": joined, "errors": errors}


async def collect_history() -> dict:
    sources = load_sources()
    client = make_client()
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")

    days = int(sources.get("history_days") or 3)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    saved = 0
    skipped = 0
    errors: list[str] = []

    for channel in watched_channels(sources):
        username, _invite, title = _source_spec(channel)
        label = username or title
        try:
            entity, resolved_title = await resolve_source(client, channel)
            _watch(entity)
            title = resolved_title or title
            async for message in client.iter_messages(entity, limit=200):
                if message.date:
                    msg_date = message.date
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                    if msg_date < since:
                        break
                ok = await _ingest_message(message, username, title, sources)
                if ok:
                    saved += 1
                else:
                    skipped += 1
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.2)
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            errors.append(f"{label}: Telegram просит подождать {wait}с")
            log.warning(
                "Telegram ограничил ResolveUsername (~%sс). Остальные каналы в этом прогоне пропускаю.",
                wait,
            )
            break
        except (UsernameNotOccupiedError, ChannelPrivateError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
            log.warning("Канал %s недоступен: %s", label, exc)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            log.exception("Ошибка сбора %s", label)

    return {"saved": saved, "skipped": skipped, "errors": errors}


async def author_profile(lead: dict) -> dict:
    """Шапка того, кто написал пост: человек или канал."""
    empty = {"about": "", "kind": ""}
    username = (lead.get("sender_username") or "").lstrip("@")
    if not username:
        return empty
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import Channel, User

        client = make_client()
        if not client.is_connected():
            await client.connect()
        entity = await client.get_entity(username)
        if isinstance(entity, User):
            full = await client(GetFullUserRequest(entity))
            about = (getattr(full.full_user, "about", None) or "").strip()
            return {"about": about, "kind": "user"}
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            about = (getattr(full.full_chat, "about", None) or "").strip()
            return {"about": about, "kind": "channel"}
        return empty
    except Exception:
        log.info("Шапку профиля автора прочитать не удалось")
        return empty


async def start_listener() -> None:
    global _listener_started
    if not await is_authorized():
        return

    await refresh_watch_list()
    if _listener_started:
        return

    client = make_client()

    @client.on(events.NewMessage)
    async def _on_new(event):
        if not _in_watch(event):
            return
        try:
            await _ingest_message(event.message, None, None, load_sources(), notify=True)
        except Exception:
            log.exception("Не удалось обработать новое сообщение")

    _listener_started = True
    log.info("Живая лента включена")


async def disconnect() -> None:
    global _client, _listener_started
    if _client is not None:
        await _client.disconnect()
        _client = None
    _listener_started = False
    _watch_ids.clear()
