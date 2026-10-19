"""Отклики на заявки из ящика без браузера: список новых, отметка «ответил», отправка в Telegram.

Запуск из apps/leads (PowerShell):

    .\.venv\Scripts\python.exe -m app.outreach list-new --limit 20
    .\.venv\Scripts\python.exe -m app.outreach mark-replied 123 --text-file reply.txt --via kwork
    .\.venv\Scripts\python.exe -m app.outreach send 123 --text-file reply.txt --mode auto
    .\.venv\Scripts\python.exe -m app.outreach send 123 --text-file reply.txt --dry-run

Отправка идёт через ту же сессию Telethon, что и сбор. Пока работает автозапуск,
сессию держит слушатель: команда просит у него паузу (session_gate) и возвращает сессию.

Защита от дублей: одна заявка — один отклик (атомарная отметка в базе),
одному автору не чаще, чем раз в author_cooldown_days. Лимиты по времени — в
config/profile.yaml, раздел outreach.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app import db
from app.filters import load_profile

OUTREACH_COLUMNS = {
    "outreach_state": "TEXT",  # sending | sent | marked | error
    "outreach_text": "TEXT",
    "outreach_via": "TEXT",  # telegram-thread | telegram-dm | kwork | manual | ...
    "outreach_at": "TEXT",
    "outreach_error": "TEXT",
    "outreach_message_id": "TEXT",
}

OUTREACH_LOG = """
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    via TEXT NOT NULL,
    peer TEXT,
    sender_id TEXT,
    sender_username TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""

DEFAULTS = {
    "mode": "auto",  # auto | thread | dm
    "min_interval_sec": 90,
    "max_per_hour": 12,
    "max_per_day": 40,
    "author_cooldown_days": 7,
    "min_fit": 3,
}

LEAD_FIELDS = (
    "id", "source", "status", "fit", "why", "suggested_offer", "high_ticket",
    "text", "url", "channel_username", "channel_title", "channel_id", "message_id",
    "message_date", "created_at", "sender_username", "sender_id", "sender_name",
    "write_url", "kind", "keywords",
)


class OutreachError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def config() -> dict:
    raw = load_profile().get("outreach") or {}
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(OUTREACH_LOG)
    cur = await conn.execute("PRAGMA table_info(leads)")
    existing = {row[1] for row in await cur.fetchall()}
    for name, typ in OUTREACH_COLUMNS.items():
        if name not in existing:
            await conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {typ}")
    await conn.commit()


async def open_db() -> aiosqlite.Connection:
    conn = await db.connect()
    await ensure_schema(conn)
    return conn


def _lead_json(row: dict) -> dict:
    item = {key: row.get(key) for key in LEAD_FIELDS}
    try:
        item["keywords"] = json.loads(row.get("keywords") or "[]")
    except (TypeError, ValueError):
        item["keywords"] = []
    author = (row.get("sender_username") or "").lstrip("@")
    item["author"] = {
        "username": f"@{author}" if author else None,
        "id": row.get("sender_id"),
        "name": row.get("sender_name"),
        "write_url": row.get("write_url") or (f"https://t.me/{author}" if author else None),
    }
    item["link"] = row.get("url")
    return item


async def list_new(
    conn: aiosqlite.Connection,
    *,
    limit: int = 30,
    min_fit: int | None = None,
    source: str | None = None,
    since_hours: int | None = None,
) -> list[dict]:
    """Новые заявки без отклика: статус new или take, отклик ещё не отправлен и не отмечен."""
    fit = config()["min_fit"] if min_fit is None else min_fit
    clauses = [
        "status IN ('new', 'take')",
        "(outreach_state IS NULL OR outreach_state = 'error')",
        "(fit IS NULL OR fit >= ?)",
    ]
    args: list = [int(fit)]
    if source:
        clauses.append("source = ?")
        args.append(source)
    if since_hours:
        clauses.append("created_at >= ?")
        args.append(_iso(_now() - timedelta(hours=since_hours)))
    args.append(int(limit))
    cur = await conn.execute(
        f"SELECT * FROM leads WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        args,
    )
    return [_lead_json(dict(row)) for row in await cur.fetchall()]


async def mark_replied(
    conn: aiosqlite.Connection,
    lead_id: int,
    text: str,
    *,
    via: str = "manual",
    message_id: str | None = None,
    force: bool = False,
) -> dict:
    """Отмечает, что отклик отправлен вне этой команды (Kwork, биржа, руками)."""
    lead = await db.get_lead(conn, lead_id)
    if not lead:
        raise OutreachError(f"заявки {lead_id} нет")
    if lead.get("outreach_state") in ("sent", "marked", "sending") and not force:
        raise OutreachError(
            f"по заявке {lead_id} отклик уже есть ({lead.get('outreach_state')}, {lead.get('outreach_at')})"
        )
    now = _iso()
    await conn.execute(
        """
        UPDATE leads
        SET outreach_state = 'marked', outreach_text = ?, outreach_via = ?, outreach_at = ?,
            outreach_error = '', outreach_message_id = ?,
            status = 'contacted', seen_at = COALESCE(seen_at, ?)
        WHERE id = ?
        """,
        (text, via, now, message_id, now, lead_id),
    )
    await conn.commit()
    return {"id": lead_id, "state": "marked", "via": via, "at": now}


async def _claim(conn: aiosqlite.Connection, lead_id: int) -> bool:
    cur = await conn.execute(
        """
        UPDATE leads SET outreach_state = 'sending', outreach_error = ''
        WHERE id = ? AND (outreach_state IS NULL OR outreach_state = 'error')
        """,
        (lead_id,),
    )
    await conn.commit()
    return cur.rowcount == 1


async def _release(conn: aiosqlite.Connection, lead_id: int, error: str) -> None:
    await conn.execute(
        "UPDATE leads SET outreach_state = 'error', outreach_error = ? WHERE id = ? AND outreach_state = 'sending'",
        (error[:500], lead_id),
    )
    await conn.commit()


async def _log(conn, lead: dict, via: str, peer: str, ok: bool, error: str = "") -> None:
    await conn.execute(
        """
        INSERT INTO outreach_log(lead_id, via, peer, sender_id, sender_username, ok, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lead["id"], via, peer, lead.get("sender_id"), lead.get("sender_username"),
            1 if ok else 0, error[:500], _iso(),
        ),
    )
    await conn.commit()


async def rate_limit_problem(
    conn: aiosqlite.Connection, lead: dict, cfg: dict | None = None, peer: str = ""
) -> str | None:
    """Почему сейчас нельзя отправить. None — можно."""
    cfg = cfg or config()
    now = _now()

    async def _count(since: datetime) -> int:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM outreach_log WHERE ok = 1 AND created_at >= ?",
            (_iso(since),),
        )
        row = await cur.fetchone()
        return int(row[0] or 0)

    cur = await conn.execute(
        "SELECT error, created_at FROM outreach_log WHERE ok = 0 AND error LIKE 'flood_wait=%' ORDER BY id DESC LIMIT 1"
    )
    flood = await cur.fetchone()
    if flood:
        try:
            seconds = int(str(flood[0]).split("=", 1)[1].split(";", 1)[0])
            until = datetime.fromisoformat(flood[1]) + timedelta(seconds=seconds)
        except (ValueError, IndexError):
            until = None
        if until and until > now:
            return f"Telegram попросил паузу (FloodWait) до {until.isoformat(timespec='seconds')}"

    cur = await conn.execute("SELECT MAX(created_at) FROM outreach_log WHERE ok = 1")
    row = await cur.fetchone()
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        wait = int(cfg["min_interval_sec"]) - int((now - last).total_seconds())
        if wait > 0:
            return f"пауза между отправками: подожди ещё {wait} с"
    if await _count(now - timedelta(hours=1)) >= int(cfg["max_per_hour"]):
        return f"лимит {cfg['max_per_hour']} отправок в час"
    if await _count(now - timedelta(days=1)) >= int(cfg["max_per_day"]):
        return f"лимит {cfg['max_per_day']} отправок в сутки"
    # Сверяем по контакту в личке: у постов из каналов sender_id — это id канала, по нему не режем.
    username = (peer or "").lstrip("@").lower()
    if username:
        since = _iso(now - timedelta(days=int(cfg["author_cooldown_days"])))
        cur = await conn.execute(
            """
            SELECT lead_id FROM outreach_log
            WHERE ok = 1 AND created_at >= ? AND lead_id != ?
              AND via = 'telegram-dm' AND lower(peer) = ?
            LIMIT 1
            """,
            (since, lead["id"], username),
        )
        hit = await cur.fetchone()
        if hit:
            return f"этому автору уже писали по заявке {hit[0]} за последние {cfg['author_cooldown_days']} дн."
    return None


def dm_username(lead: dict) -> str:
    """Кому писать в личку: контакт из write_url (автор-человек или @ из текста заявки)."""
    url = (lead.get("write_url") or "").strip()
    if url.startswith("https://t.me/") or url.startswith("http://t.me/"):
        name = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        if name and name.lower() != (lead.get("channel_username") or "").lstrip("@").lower():
            return name
    return ""


def plan_target(lead: dict, mode: str) -> tuple[str, str]:
    """Куда писать: ('telegram-dm', @контакт) или ('telegram-thread', чат)."""
    if (lead.get("source") or "telegram") != "telegram":
        raise OutreachError(
            f"заявка из {lead.get('source')}: отправь отклик на бирже и отметь командой mark-replied"
        )
    contact = dm_username(lead)
    chat = (lead.get("channel_username") or "").lstrip("@") or str(lead.get("channel_id") or "")
    has_thread = bool(chat and lead.get("message_id"))
    if mode == "dm":
        if not contact:
            raise OutreachError("у заявки нет контакта для личного сообщения")
        return "telegram-dm", contact
    if mode == "thread":
        if not has_thread:
            raise OutreachError("у заявки нет чата и номера сообщения для ответа в треде")
        return "telegram-thread", chat
    if contact:
        return "telegram-dm", contact
    if has_thread:
        return "telegram-thread", chat
    raise OutreachError("некуда писать: нет ни контакта, ни чата")


async def _telegram_send(via: str, peer: str, lead: dict, text: str) -> str:
    from app.collector import disconnect, make_client
    from app.servicekit import runtime_dir
    from app.session_gate import borrow_session

    async with borrow_session(runtime_dir(), timeout=60):
        client = make_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise OutreachError("сессия Telegram не авторизована, войди через http://127.0.0.1:8787")
            target = int(peer) if peer.lstrip("-").isdigit() else peer
            entity = await client.get_entity(target)
            if via == "telegram-thread":
                msg = await client.send_message(entity, text, reply_to=int(lead["message_id"]))
            else:
                msg = await client.send_message(entity, text)
            return str(getattr(msg, "id", ""))
        finally:
            await disconnect()


async def send_reply(
    conn: aiosqlite.Connection,
    lead_id: int,
    text: str,
    *,
    mode: str | None = None,
    dry_run: bool = False,
    sender=None,
) -> dict:
    text = (text or "").strip()
    if len(text) < 10:
        raise OutreachError("текст отклика пустой или слишком короткий")
    cfg = config()
    lead = await db.get_lead(conn, lead_id)
    if not lead:
        raise OutreachError(f"заявки {lead_id} нет")
    if lead.get("outreach_state") in ("sent", "marked", "sending"):
        raise OutreachError(f"по заявке {lead_id} отклик уже есть ({lead.get('outreach_state')})")
    via, peer = plan_target(lead, (mode or cfg["mode"]).lower())
    problem = await rate_limit_problem(conn, lead, cfg, peer if via == "telegram-dm" else "")
    if problem:
        raise OutreachError(problem)
    if dry_run:
        return {"id": lead_id, "dry_run": True, "via": via, "peer": peer, "text": text}
    if not await _claim(conn, lead_id):
        raise OutreachError(f"по заявке {lead_id} отклик уже отправляется или отправлен")
    send = sender or _telegram_send
    try:
        message_id = await send(via, peer, lead, text)
    except Exception as exc:  # FloodWait, приватность, бан в чате и т.п.
        error = f"{type(exc).__name__}: {exc}"
        seconds = getattr(exc, "seconds", None)
        if isinstance(seconds, int):
            error = f"flood_wait={seconds}; {error}"
        await _release(conn, lead_id, error)
        await _log(conn, lead, via, peer, False, error)
        raise OutreachError(f"не отправилось: {error}") from exc
    now = _iso()
    await conn.execute(
        """
        UPDATE leads
        SET outreach_state = 'sent', outreach_text = ?, outreach_via = ?, outreach_at = ?,
            outreach_error = '', outreach_message_id = ?,
            status = 'contacted', seen_at = COALESCE(seen_at, ?)
        WHERE id = ?
        """,
        (text, via, now, message_id, now, lead_id),
    )
    await conn.commit()
    await _log(conn, lead, via, peer, True)
    return {"id": lead_id, "state": "sent", "via": via, "peer": peer, "message_id": message_id, "at": now}


def _read_text(args) -> str:
    if getattr(args, "text_file", None):
        return Path(args.text_file).read_text(encoding="utf-8-sig")
    if getattr(args, "text", None) == "-":
        return sys.stdin.read()
    return getattr(args, "text", None) or ""


def _print(data) -> None:
    out = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(out)


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.outreach", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-new", help="новые заявки без отклика, JSON")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--min-fit", type=int, default=None)
    p_list.add_argument("--source", default=None, help="telegram, kwork, hh, avito ...")
    p_list.add_argument("--since-hours", type=int, default=None)

    for name, help_text in (("mark-replied", "отметить, что отклик отправлен"), ("send", "отправить отклик в Telegram")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("lead_id", type=int)
        p.add_argument("--text", default=None, help="текст отклика или '-' для stdin")
        p.add_argument("--text-file", default=None, help="файл с текстом в UTF-8")
        if name == "mark-replied":
            p.add_argument("--via", default="manual")
            p.add_argument("--message-id", default=None)
            p.add_argument("--force", action="store_true")
        else:
            p.add_argument("--mode", choices=("auto", "thread", "dm"), default=None)
            p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    conn = await open_db()
    try:
        if args.cmd == "list-new":
            _print(await list_new(conn, limit=args.limit, min_fit=args.min_fit, source=args.source, since_hours=args.since_hours))
        elif args.cmd == "mark-replied":
            _print(await mark_replied(conn, args.lead_id, _read_text(args).strip(), via=args.via, message_id=args.message_id, force=args.force))
        else:
            _print(await send_reply(conn, args.lead_id, _read_text(args), mode=args.mode, dry_run=args.dry_run))
    except OutreachError as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    finally:
        await conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
