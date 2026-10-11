"""Join public Telegram chats one by one; append successes to sources.yaml."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles often use cp1251; chat titles may contain emoji.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _safe_print(*args, **kwargs) -> None:
    kwargs.setdefault("flush", True)
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        print(text.encode("utf-8", "replace").decode("utf-8", "replace"), **kwargs)

from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    InviteRequestSentError,
    PeerFloodError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.settings import settings  # noqa: E402

SOURCES = settings.sources_path
REPORT = ROOT / "data" / "join-report.json"
JOIN_LOCK = ROOT / "data" / "JOIN_IN_PROGRESS"
STRING_SESSION = ROOT / "data" / "telegram.string"

USERNAMES = [
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

DONE_STATUSES = {
    "joined",
    "already_member",
    "request_sent",
    "private",
    "not_found",
    "banned",
    "invalid",
}


def already_in_sources(username: str) -> bool:
    text = SOURCES.read_text(encoding="utf-8")
    return bool(re.search(rf"^\s*- username:\s*{re.escape(username)}\s*$", text, re.M))


def append_source(username: str, title: str) -> None:
    if already_in_sources(username):
        return
    block = (
        f"\n  # Вступление скриптом, 2026-10-11\n"
        f"  - username: {username}\n"
        f"    title: {json.dumps(title, ensure_ascii=False)}\n"
    )
    with SOURCES.open("a", encoding="utf-8") as f:
        f.write(block)


def load_report() -> list[dict]:
    if not REPORT.exists():
        return []
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_report(report: list[dict]) -> None:
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_report(report: list[dict], row: dict) -> list[dict]:
    out = [r for r in report if r.get("username") != row["username"]]
    out.append(row)
    order = {u: i for i, u in enumerate(USERNAMES)}
    out.sort(key=lambda r: order.get(r.get("username", ""), 999))
    return out


def pending_usernames(report: list[dict]) -> list[str]:
    done = {r["username"] for r in report if r.get("status") in DONE_STATUSES}
    final = []
    for u in USERNAMES:
        if u in done:
            continue
        if already_in_sources(u):
            continue
        final.append(u)
    return final


def kill_leads_app() -> None:
    """Stop inbox processes so they release the Telegram session."""
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match '-m app' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return
    for line in out.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if pid == my_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _safe_print(f"  stopped leads pid {pid}")
        except OSError:
            pass


def acquire_lock() -> None:
    JOIN_LOCK.write_text(f"pid={os.getpid()} started={time.time()}\n", encoding="utf-8")


def release_lock() -> None:
    try:
        JOIN_LOCK.unlink(missing_ok=True)
    except TypeError:
        if JOIN_LOCK.exists():
            JOIN_LOCK.unlink()


async def join_one(client: TelegramClient, username: str) -> dict:
    uname = username.lstrip("@")
    result = {
        "username": uname,
        "status": "error",
        "title": None,
        "detail": "",
        "added_to_sources": False,
    }
    while True:
        try:
            entity = await client.get_entity(uname)
            title = getattr(entity, "title", None) or uname
            result["title"] = title
            try:
                await client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                result["status"] = "already_member"
                result["detail"] = "уже был участником"
            else:
                result["status"] = "joined"
                result["detail"] = "вступил"
            append_source(uname, title)
            result["added_to_sources"] = True
            return result
        except FloodWaitError as e:
            wait = int(e.seconds) + 2
            _safe_print(f"  FloodWait {wait}s for @{uname}…")
            await asyncio.sleep(wait)
            continue
        except InviteRequestSentError:
            result["status"] = "request_sent"
            result["detail"] = "заявка на вход отправлена"
            return result
        except ChannelPrivateError:
            result["status"] = "private"
            result["detail"] = "приватный / отказ без заявки"
            return result
        except (UsernameNotOccupiedError, UsernameInvalidError):
            result["status"] = "not_found"
            result["detail"] = "username не существует"
            return result
        except UserBannedInChannelError:
            result["status"] = "banned"
            result["detail"] = "аккаунт забанен в чате"
            return result
        except PeerFloodError:
            result["status"] = "peer_flood"
            result["detail"] = "Telegram ограничил вступления (PeerFlood)"
            return result
        except ChannelInvalidError:
            result["status"] = "invalid"
            result["detail"] = "канал недоступен"
            return result
        except Exception as e:
            name = type(e).__name__
            if "InviteRequestSent" in name:
                result["status"] = "request_sent"
                result["detail"] = "заявка на вход отправлена"
                return result
            result["status"] = "error"
            result["detail"] = f"{name}: {e}"
            return result


async def main() -> int:
    if not settings.has_telegram_api:
        print("ERROR: TELEGRAM_API_ID / HASH missing", flush=True)
        return 1
    if not STRING_SESSION.exists():
        print("ERROR: data/telegram.string missing — export session first", flush=True)
        return 1

    report = load_report()
    report = [
        r
        for r in report
        if not (
            r.get("status") == "error"
            and "locked" in (r.get("detail") or "").lower()
        )
        and r.get("status") != "skipped"
    ]
    save_report(report)

    pending = pending_usernames(report)
    _safe_print(f"Pending: {len(pending)} / {len(USERNAMES)}")
    if not pending:
        _safe_print("Nothing to do")
        _safe_print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    acquire_lock()
    kill_leads_app()
    await asyncio.sleep(2)

    session = StringSession(STRING_SESSION.read_text(encoding="utf-8").strip())
    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("ERROR: string session not authorized", flush=True)
            return 1
        me = await client.get_me()
        _safe_print(f"Logged in as {me.username or me.id}")

        stop_rest = False
        for i, uname in enumerate(pending):
            if stop_rest:
                row = {
                    "username": uname,
                    "status": "skipped",
                    "title": None,
                    "detail": "пропущен из‑за PeerFlood раньше",
                    "added_to_sources": False,
                }
                report = upsert_report(report, row)
                continue
            kill_leads_app()
            _safe_print(f"[{i + 1}/{len(pending)}] @{uname} …")
            row = await join_one(client, uname)
            report = upsert_report(report, row)
            save_report(report)
            title = row.get("title") or ""
            _safe_print(f"  -> {row['status']}: {row['detail']} | {title}")
            if row["status"] == "peer_flood":
                stop_rest = True
                continue
            if i < len(pending) - 1:
                pause = random.randint(60, 120)
                _safe_print(f"  pause {pause}s")
                # Keep inbox from reclaiming the session during the pause.
                end = time.time() + pause
                while time.time() < end:
                    kill_leads_app()
                    await asyncio.sleep(min(15, end - time.time()))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_lock()

    _safe_print("DONE")
    _safe_print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
