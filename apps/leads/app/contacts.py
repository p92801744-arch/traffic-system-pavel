from __future__ import annotations

import logging
import re

from telethon.tl.types import Channel, Chat, User

log = logging.getLogger("leads.contacts")

VACANCY_MARKERS = (
    "вакансия",
    "вакансии",
    "ищем в команду",
    "ищем сотрудника",
    "требуется сотрудник",
    "full-time",
    "полная занятость",
    "в штат",
    "оформление в штат",
)

USERNAME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,})|@([A-Za-z][A-Za-z0-9_]{4,})")


def classify_kind(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in VACANCY_MARKERS):
        return "vacancy"
    return "order"


def _username_from_entity(entity) -> str | None:
    return getattr(entity, "username", None) or None


def _display_name(entity) -> str | None:
    if entity is None:
        return None
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    return name or getattr(entity, "title", None)


def _write_url(username: str | None) -> str | None:
    if not username:
        return None
    return f"https://t.me/{username.lstrip('@')}"


def usernames_in_text(text: str, skip: set[str] | None = None) -> list[str]:
    skip = {s.lower() for s in (skip or set())}
    found: list[str] = []
    for match in USERNAME_RE.finditer(text or ""):
        name = (match.group(1) or match.group(2) or "").lstrip("@")
        if not name or name.lower() in skip:
            continue
        if name.lower() in {"joinchat", "addstickers", "share", "proxy", "socks"}:
            continue
        if name not in found:
            found.append(name)
    return found


async def extract_contact(message, channel_username: str | None = None) -> dict:
    sender = await message.get_sender()
    sender_username = _username_from_entity(sender)
    sender_id = getattr(sender, "id", None)
    sender_name = _display_name(sender)
    write_username = sender_username if isinstance(sender, User) else None

    fwd = getattr(message, "forward", None)
    if fwd is not None and not write_username:
        fwd_sender = getattr(fwd, "sender", None)
        if isinstance(fwd_sender, User) and getattr(fwd_sender, "username", None):
            write_username = fwd_sender.username
            sender_username = sender_username or fwd_sender.username
            sender_id = sender_id or getattr(fwd_sender, "id", None)
            sender_name = sender_name or _display_name(fwd_sender)

    skip = {u.lower() for u in [channel_username, sender_username] if u}
    if not write_username:
        mentions = usernames_in_text(message.message or "", skip=skip)
        if mentions:
            write_username = mentions[0]

    return {
        "sender_username": sender_username,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "write_url": _write_url(write_username),
        "kind": classify_kind(message.message or ""),
        "is_user": isinstance(sender, User),
        "is_channel": isinstance(sender, (Channel, Chat)),
    }
