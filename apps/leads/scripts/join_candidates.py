"""Вступить в чаты-кандидаты с сессии Telethon. В чаты ничего не пишет.

Запуск с компьютера, где лежат apps/leads/.env и data/telegram.session:

    cd apps/leads
    .\\.venv\\Scripts\\python.exe scripts\\join_candidates.py

Между вступлениями пауза 60–120 секунд. При FloodWait ждёт, сколько сказал Telegram.
После успешного вступления (или если уже участник) убирает join: candidate в sources.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

import yaml
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    InviteRequestSentError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collector import make_client  # noqa: E402
from app.session_gate import SessionBusy, ensure, release  # noqa: E402
from app.settings import settings  # noqa: E402

DEFAULT_ORDER = [
    "marketplace_wbchat",
    "ozon_chat",
    "biznes_chat",
    "n8n_community",
    "chat_wildberries",
    "wb_help",
    "chat_ozon",
    "chatsellerov",
    "ozon_community",
    "marketplace_chat",
    "biznes_piter",
    "tilda_chat",
    "zapier_ru",
    "it_serbia",
    "tbilisi_it",
    "armenia_it",
    "it_almaty",
    "it_belgrade",
]


def _load_sources() -> dict:
    return yaml.safe_load(settings.sources_path.read_text(encoding="utf-8")) or {}


def _channel_map(data: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for channel in data.get("channels") or []:
        username = (channel.get("username") or "").lstrip("@").lower()
        if username:
            found[username] = channel
    return found


def activate_channel(username: str) -> None:
    """Убирает join: candidate и why у одного чата, комментарии файла не трогает."""
    path = settings.sources_path
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    name = username.lstrip("@")
    start = None
    for index, line in enumerate(lines):
        if line.startswith(f"  - username: {name}") or line.startswith(f"  - username: {name}\n"):
            start = index
            break
        if line.rstrip("\r\n") == f"  - username: {name}":
            start = index
            break
    if start is None:
        for index, line in enumerate(lines):
            if f"username: {name}" in line and line.lstrip().startswith("- username:"):
                start = index
                break
    if start is None:
        return
    end = start + 1
    while end < len(lines):
        line = lines[end]
        # Следующий пункт списка или новый корневой ключ YAML.
        if line.startswith("  - ") or (line.strip() and not line.startswith((" ", "#", "\t"))):
            break
        end += 1
    kept = []
    removed = False
    for line in lines[start:end]:
        stripped = line.strip()
        if stripped.startswith("join:") or stripped.startswith("why:"):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return
    path.write_text("".join(lines[:start] + kept + lines[end:]), encoding="utf-8")


async def join_one(client, username: str) -> str:
    """joined | already | request | private | missing | error:..."""
    try:
        entity = await client.get_entity(username)
    except UsernameNotOccupiedError:
        return "missing"
    except UsernameInvalidError:
        return "missing"
    except ValueError:
        return "missing"
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 0) or 0) + 2
        print(f"  Telegram просит подождать {wait}с перед поиском @{username}")
        await asyncio.sleep(wait)
        return await join_one(client, username)

    try:
        await client(JoinChannelRequest(entity))
        return "joined"
    except UserAlreadyParticipantError:
        return "already"
    except InviteRequestSentError:
        return "request"
    except ChannelPrivateError:
        return "private"
    except ChannelInvalidError:
        return "private"
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 0) or 0) + 2
        print(f"  Telegram просит подождать {wait}с перед вступлением в @{username}")
        await asyncio.sleep(wait)
        return await join_one(client, username)
    except Exception as exc:
        text = str(exc).lower()
        if "already" in text and "participant" in text:
            return "already"
        if "request" in text and "sent" in text:
            return "request"
        if "private" in text or "forbidden" in text:
            return "private"
        return f"error:{exc}"


async def main(usernames: list[str], pause_min: int, pause_max: int) -> int:
    if not settings.has_telegram_api:
        print("Нет TELEGRAM_API_ID / TELEGRAM_API_HASH в apps/leads/.env")
        return 2
    if not settings.session_file_exists:
        print(f"Нет файла сессии: {settings.session_path}.session")
        print("Сначала войди в ящик лидов на этом компьютере и подтверди код Telegram.")
        return 2

    try:
        ensure("join")
    except SessionBusy as exc:
        print(f"Сессию Telegram уже держит другой процесс ({exc}).")
        print("Второй клиент не открываю. Дождись, пока ящик лидов или прогон рейтинга отпустит сессию.")
        return 3

    client = make_client()
    try:
        return await _join_all(client, usernames, pause_min, pause_max)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release()


async def _join_all(client, usernames: list[str], pause_min: int, pause_max: int) -> int:
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        print("Сессия Telegram есть, но вход не подтверждён. Открой ящик лидов и войди ещё раз.")
        return 2

    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "first_name", None) or me.id
    print(f"Вошёл как {label}. В чаты писать не буду, только вступление.")

    sources = _channel_map(_load_sources())
    report = {"joined": [], "already": [], "request": [], "private": [], "missing": [], "error": []}

    for index, username in enumerate(usernames):
        username = username.lstrip("@").lower()
        meta = sources.get(username) or {"username": username, "title": username}
        title = meta.get("title") or username
        print(f"\n[{index + 1}/{len(usernames)}] @{username} — {title}")
        status = await join_one(client, username)
        if status in {"joined", "already"}:
            activate_channel(username)
            report[status].append(username)
            print(f"  {'Вступил' if status == 'joined' else 'Уже был внутри'}. Источник активен в sources.yaml.")
        elif status == "request":
            report["request"].append(username)
            print("  Заявка на вход отправлена. Жди одобрения, потом запусти скрипт ещё раз.")
        elif status == "private":
            report["private"].append(username)
            print("  Чат закрыт или нужен инвайт. Без ссылки-приглашения не войти.")
        elif status == "missing":
            report["missing"].append(username)
            print("  Такого username нет.")
        else:
            report["error"].append(f"{username}: {status}")
            print(f"  Отказ: {status}")

        if index + 1 < len(usernames):
            pause = random.randint(pause_min, pause_max)
            print(f"  Пауза {pause}с")
            await asyncio.sleep(pause)

    print("\n=== Отчёт ===")
    print("Вступил:", ", ".join(f"@{u}" for u in report["joined"]) or "—")
    print("Уже был:", ", ".join(f"@{u}" for u in report["already"]) or "—")
    print("Нужна заявка / ждёт одобрения:", ", ".join(f"@{u}" for u in report["request"]) or "—")
    print("Закрыт / нужен инвайт:", ", ".join(f"@{u}" for u in report["private"]) or "—")
    print("Нет такого чата:", ", ".join(f"@{u}" for u in report["missing"]) or "—")
    print("Ошибка:", "; ".join(report["error"]) or "—")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Вступить в чаты-кандидаты без сообщений")
    parser.add_argument(
        "usernames",
        nargs="*",
        help="Список @чатов. Если пусто — порядок из запроса Павла.",
    )
    parser.add_argument("--pause-min", type=int, default=60)
    parser.add_argument("--pause-max", type=int, default=120)
    args = parser.parse_args()
    names = [name.lstrip("@") for name in (args.usernames or DEFAULT_ORDER)]
    raise SystemExit(asyncio.run(main(names, args.pause_min, args.pause_max)))
