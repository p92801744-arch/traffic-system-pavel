from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.filters import prefix_fingerprint, text_fingerprint
from app.settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'telegram',
    channel_username TEXT,
    channel_title TEXT,
    channel_id TEXT,
    message_id INTEGER,
    message_date TEXT,
    text TEXT NOT NULL,
    url TEXT,
    keywords TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    fit INTEGER,
    is_paid_order INTEGER,
    why TEXT,
    suggested_offer TEXT,
    high_ticket INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(source, channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hidden_keys (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_on TEXT,
    when_label TEXT,
    place TEXT,
    url TEXT,
    topic TEXT,
    why TEXT,
    title_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
"""


def _source_key(payload: dict) -> str:
    source = (payload.get("source") or "telegram").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    message_id = payload.get("message_id")
    if channel_id and message_id is not None:
        return f"{source}:{channel_id}:{message_id}"
    return ""


def _lead_block_keys(row: dict) -> list[str]:
    keys: list[str] = []
    text = row.get("text") or ""
    fingerprint = (row.get("text_key") or "").strip() or text_fingerprint(text)
    prefix = (row.get("prefix_key") or "").strip() or prefix_fingerprint(text)
    if fingerprint:
        keys.append(fingerprint)
    if prefix:
        keys.append(prefix)
    source_key = _source_key(row)
    if source_key:
        keys.append(source_key)
    return list(dict.fromkeys(keys))

LEAD_EXTRA_COLUMNS = {
    "sender_username": "TEXT",
    "sender_id": "TEXT",
    "sender_name": "TEXT",
    "write_url": "TEXT",
    "kind": "TEXT",
    "reject_reason": "TEXT",
    "text_key": "TEXT",
    "prefix_key": "TEXT",
    "reply_text": "TEXT",
    "reply_state": "TEXT",
    "reply_error": "TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def connect() -> aiosqlite.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.executescript(SCHEMA)
    cur = await db.execute("PRAGMA table_info(leads)")
    existing = {row[1] for row in await cur.fetchall()}
    for name, typ in LEAD_EXTRA_COLUMNS.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE leads ADD COLUMN {name} {typ}")
    await db.commit()
    return db


async def remember_hidden(db: aiosqlite.Connection, keys: list[str]) -> None:
    """Помечает отпечатки, чтобы удалённые вакансии не вернулись после сбора."""
    now = _now()
    for key in keys:
        key = (key or "").strip()
        if not key:
            continue
        await db.execute(
            "INSERT OR IGNORE INTO hidden_keys(key, created_at) VALUES (?, ?)",
            (key, now),
        )
    await db.commit()


async def is_hidden(db: aiosqlite.Connection, keys: list[str]) -> bool:
    clean = [k for k in keys if k]
    if not clean:
        return False
    marks = ",".join("?" for _ in clean)
    cur = await db.execute(
        f"SELECT 1 FROM hidden_keys WHERE key IN ({marks}) LIMIT 1",
        clean,
    )
    return await cur.fetchone() is not None


async def upsert_lead(db: aiosqlite.Connection, payload: dict) -> int | None:
    """Возвращает id, если строка новая. None — это сообщение или такой же текст уже есть."""
    fingerprint = text_fingerprint(payload["text"])
    prefix = prefix_fingerprint(payload["text"])
    source_key = _source_key(payload)
    if await is_hidden(db, [fingerprint, prefix, source_key]):
        return None
    existing = await db.execute(
        "SELECT id FROM leads WHERE text_key = ? OR prefix_key = ?",
        (fingerprint, prefix),
    )
    if await existing.fetchone():
        return None
    cur = await db.execute(
        """
        INSERT OR IGNORE INTO leads (
            source, channel_username, channel_title, channel_id, message_id,
            message_date, text, url, keywords, created_at,
            sender_username, sender_id, sender_name, write_url, kind, text_key, prefix_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("source", "telegram"),
            payload.get("channel_username"),
            payload.get("channel_title"),
            str(payload.get("channel_id") or ""),
            payload.get("message_id"),
            payload.get("message_date"),
            payload["text"],
            payload.get("url"),
            json.dumps(payload.get("keywords") or [], ensure_ascii=False),
            _now(),
            payload.get("sender_username"),
            str(payload.get("sender_id") or "") or None,
            payload.get("sender_name"),
            payload.get("write_url"),
            payload.get("kind"),
            fingerprint,
            prefix,
        ),
    )
    await db.commit()
    if cur.rowcount and cur.lastrowid:
        return int(cur.lastrowid)
    return None


async def list_leads(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    statuses: list[str] | None = None,
    min_fit: int | None = None,
    hide_low: bool = True,
    channel: str | None = None,
    limit: int = 80,
) -> list[dict]:
    clauses = ["1=1"]
    args: list = []
    if statuses:
        marks = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({marks})")
        args.extend(statuses)
    elif status:
        clauses.append("status = ?")
        args.append(status)
    if channel:
        clauses.append("channel_username = ?")
        args.append(channel)
    if min_fit is not None:
        clauses.append("(fit IS NULL OR fit >= ?)")
        args.append(min_fit)
    elif hide_low:
        clauses.append("(fit IS NULL OR fit >= 3)")
    sql = f"""
        SELECT * FROM leads
        WHERE {' AND '.join(clauses)}
        ORDER BY message_date DESC, id DESC
        LIMIT ?
    """
    args.append(limit)
    cur = await db.execute(sql, args)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_lead(db: aiosqlite.Connection, lead_id: int) -> dict | None:
    cur = await db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_status(db: aiosqlite.Connection, lead_id: int, status: str) -> None:
    await db.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    await db.commit()


async def mark_reply(db: aiosqlite.Connection, lead_id: int, state: str, error: str = "") -> None:
    await db.execute(
        "UPDATE leads SET reply_state = ?, reply_error = ? WHERE id = ?",
        (state, error, lead_id),
    )
    await db.commit()


async def save_reply(db: aiosqlite.Connection, lead_id: int, text: str) -> None:
    await db.execute(
        "UPDATE leads SET reply_text = ?, reply_state = 'ready', reply_error = '' WHERE id = ?",
        (text, lead_id),
    )
    await db.commit()


async def reset_writing_replies(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        UPDATE leads
        SET reply_state = 'error',
            reply_error = 'запись прервалась, открой отклик и попроси написать ещё раз'
        WHERE reply_state = 'writing'
        """
    )
    await db.commit()


async def reject_lead(db: aiosqlite.Connection, lead_id: int, reason: str) -> None:
    reason = (reason or "").strip()
    await db.execute(
        """
        UPDATE leads
        SET status = 'reject',
            reject_reason = ?,
            fit = CASE WHEN fit IS NULL OR fit > 2 THEN 2 ELSE fit END
        WHERE id = ?
        """,
        (reason, lead_id),
    )
    await db.commit()


async def recent_reject_reasons(db: aiosqlite.Connection, limit: int = 25) -> list[str]:
    cur = await db.execute(
        """
        SELECT reject_reason FROM leads
        WHERE status = 'reject'
          AND reject_reason IS NOT NULL
          AND trim(reject_reason) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [str(r[0]).strip() for r in await cur.fetchall() if r[0]]


async def save_score(db: aiosqlite.Connection, lead_id: int, score: dict, *, commit: bool = True) -> None:
    await db.execute(
        """
        UPDATE leads
        SET fit = ?, is_paid_order = ?, why = ?, suggested_offer = ?, high_ticket = ?
        WHERE id = ?
        """,
        (
            score.get("fit"),
            1 if score.get("is_paid_order") else 0,
            score.get("why"),
            score.get("suggested_offer"),
            1 if score.get("high_ticket") else 0,
            lead_id,
        ),
    )
    if commit:
        await db.commit()


async def delete_leads(
    db: aiosqlite.Connection,
    lead_ids: list[int],
    *,
    remember: bool = False,
) -> None:
    if not lead_ids:
        return
    if remember:
        keys: list[str] = []
        for start in range(0, len(lead_ids), 400):
            chunk = lead_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            cur = await db.execute(
                f"SELECT source, channel_id, message_id, text, text_key, prefix_key "
                f"FROM leads WHERE id IN ({placeholders})",
                chunk,
            )
            for row in await cur.fetchall():
                keys.extend(_lead_block_keys(dict(row)))
        await remember_hidden(db, keys)
    for start in range(0, len(lead_ids), 400):
        chunk = lead_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        await db.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", chunk)
    await db.commit()


async def delete_taken(db: aiosqlite.Connection) -> int:
    cur = await db.execute(
        "SELECT id, source, channel_id, message_id, text, text_key, prefix_key "
        "FROM leads WHERE status IN ('take', 'contacted')"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    keys: list[str] = []
    for row in rows:
        keys.extend(_lead_block_keys(row))
    await remember_hidden(db, keys)
    cur = await db.execute(
        "DELETE FROM leads WHERE status IN ('take', 'contacted')"
    )
    await db.commit()
    return int(cur.rowcount or 0)


async def duplicate_lead_ids(db: aiosqlite.Connection) -> list[int]:
    """Повтор того же текста или того же начала: оставляем первую карточку."""
    cur = await db.execute("SELECT id, text, text_key, prefix_key FROM leads ORDER BY id")
    seen: set[str] = set()
    drop: list[int] = []
    for row in await cur.fetchall():
        keys = row.keys()
        fingerprint = row["text_key"] or text_fingerprint(row["text"] or "")
        prefix = row["prefix_key"] if "prefix_key" in keys and row["prefix_key"] else prefix_fingerprint(row["text"] or "")
        if not row["text_key"] or ("prefix_key" in keys and not row["prefix_key"]):
            await db.execute(
                "UPDATE leads SET text_key = ?, prefix_key = ? WHERE id = ?",
                (fingerprint, prefix, row["id"]),
            )
        if fingerprint in seen or prefix in seen:
            drop.append(int(row["id"]))
        else:
            seen.add(fingerprint)
            seen.add(prefix)
    await db.commit()
    return drop


async def leads_for_purge(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        """
        SELECT id, text, status
        FROM leads
        WHERE status NOT IN ('take', 'contacted')
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def leads_for_rescore(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        """
        SELECT id, text, channel_username
        FROM leads
        WHERE status != 'reject'
        """
    )
    return [dict(r) for r in await cur.fetchall()]


async def unscored(db: aiosqlite.Connection, limit: int = 10) -> list[dict]:
    cur = await db.execute(
        """
        SELECT * FROM leads
        WHERE fit IS NULL AND status = 'new'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def channels(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute(
        """
        SELECT DISTINCT channel_username
        FROM leads
        WHERE channel_username IS NOT NULL AND channel_username != ''
        ORDER BY channel_username
        """
    )
    return [r[0] for r in await cur.fetchall()]


async def stats(db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS new_count,
            SUM(CASE WHEN status = 'take' THEN 1 ELSE 0 END) AS take_count,
            SUM(CASE WHEN status = 'contacted' THEN 1 ELSE 0 END) AS contacted_count,
            SUM(CASE WHEN high_ticket = 1 THEN 1 ELSE 0 END) AS high_ticket_count
        FROM leads
        """
    )
    row = dict(await cur.fetchone())
    return {k: int(v or 0) for k, v in row.items()}


async def upsert_event(db: aiosqlite.Connection, payload: dict) -> bool:
    """Кладёт встречу. Та же дата и то же название из другого источника не дублируются."""
    title_key = (payload.get("title_key") or "").strip()
    starts_on = (payload.get("starts_on") or "").strip()
    source = payload.get("source") or ""
    external_id = str(payload.get("external_id") or "")
    if not title_key or not starts_on or not external_id:
        return False
    cur = await db.execute(
        "SELECT id, source, external_id FROM events WHERE title_key = ? AND starts_on = ?",
        (title_key, starts_on),
    )
    twin = await cur.fetchone()
    if twin and (twin["source"] != source or str(twin["external_id"]) != external_id):
        return False
    await db.execute(
        """
        INSERT INTO events (
            source, external_id, title, starts_on, when_label, place, url, topic, why, title_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, external_id) DO UPDATE SET
            title = excluded.title,
            starts_on = excluded.starts_on,
            when_label = excluded.when_label,
            place = excluded.place,
            url = excluded.url,
            topic = excluded.topic,
            why = excluded.why,
            title_key = excluded.title_key
        """,
        (
            source,
            external_id,
            payload.get("title") or "",
            starts_on,
            payload.get("when_label") or "",
            payload.get("place") or "",
            payload.get("url") or "",
            payload.get("topic") or "",
            payload.get("why") or "",
            title_key,
            _now(),
        ),
    )
    await db.commit()
    return True


async def delete_past_events(db: aiosqlite.Connection, today: str) -> None:
    await db.execute(
        "DELETE FROM events WHERE starts_on IS NULL OR starts_on < ?",
        (today,),
    )
    await db.commit()


async def list_events(db: aiosqlite.Connection, today: str, limit: int = 40) -> list[dict]:
    cur = await db.execute(
        """
        SELECT * FROM events
        WHERE starts_on >= ?
        ORDER BY starts_on, id
        LIMIT ?
        """,
        (today, limit),
    )
    return [dict(row) for row in await cur.fetchall()]


async def add_subscriber(db: aiosqlite.Connection, chat_id: int, username: str | None = None) -> None:
    await db.execute(
        """
        INSERT INTO subscribers(chat_id, username, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username
        """,
        (chat_id, username, _now()),
    )
    await db.commit()


async def list_subscribers(db: aiosqlite.Connection) -> list[int]:
    cur = await db.execute("SELECT chat_id FROM subscribers")
    return [int(r[0]) for r in await cur.fetchall()]


def session_path() -> Path:
    return Path(f"{settings.session_path}.session")
