from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import db, lessons
from app.settings import settings

log = logging.getLogger("leads.notifier")

_poll_task: asyncio.Task | None = None
_digest_task: asyncio.Task | None = None
_offset = 0
_pending_reject: dict[int, int] = {}


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.bot_token}/{method}"


def enabled() -> bool:
    return bool(settings.bot_token)


def _kind_label(kind: str | None) -> str:
    return "Вакансия" if kind == "vacancy" else "Заказ"


def _format_lead(lead: dict) -> str:
    text = html.escape((lead.get("text") or "").strip())
    if len(text) > 900:
        text = text[:900] + "…"
    channel = html.escape(lead.get("channel_username") or lead.get("channel_title") or "канал")
    title = html.escape(lead.get("channel_title") or "")
    sender = html.escape(lead.get("sender_name") or lead.get("sender_username") or "")
    kind = _kind_label(lead.get("kind"))
    lines = [f"<b>{kind}</b> · @{channel}"]
    if title and title.lower() != channel.lower():
        lines.append(html.escape(title))
    if sender:
        lines.append(f"Автор: {sender}")
    if lead.get("fit"):
        extra = " · высокий чек" if lead.get("high_ticket") else ""
        lines.append(f"Оценка: {lead['fit']}/5{extra}")
    if lead.get("why"):
        prefix = "Почему отдал: " if (lead.get("fit") or 0) >= 3 else "Почему слабый: "
        lines.append(prefix + html.escape(lead["why"]))
    lines.append("")
    lines.append(text)
    return "\n".join(lines)


def _keyboard(lead: dict) -> dict:
    rows: list[list[dict]] = []
    if lead.get("url"):
        rows.append([{"text": "Открыть в Telegram", "url": lead["url"]}])
    actions = []
    if lead.get("write_url"):
        actions.append({"text": "Написать", "url": lead["write_url"]})
    else:
        actions.append({"text": "Написать", "callback_data": f"w:{lead['id']}"})
    actions.append({"text": "Откликнуться", "callback_data": f"a:{lead['id']}"})
    actions.append({"text": "Не моё", "callback_data": f"n:{lead['id']}"})
    rows.append(actions)
    rows.append([{"text": "Написать отклик", "callback_data": f"r:{lead['id']}"}])
    return {"inline_keyboard": rows}


async def _post(method: str, payload: dict) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(_api(method), json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or resp.text)
        return data.get("result") or {}


async def _answer_callback(callback_id: str, text: str, *, alert: bool = False) -> None:
    try:
        await _post(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text, "show_alert": alert},
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "too old" in msg or "query_id_invalid" in msg or "query is too old" in msg:
            log.info("Кнопка бота уже протухла: %s", exc)
            return
        raise


async def send_lead(lead: dict, *, min_fit: int = 3) -> None:
    if not enabled():
        return
    fit = lead.get("fit")
    if fit is None or int(fit) < min_fit:
        return
    conn = await db.connect()
    try:
        chats = await db.list_subscribers(conn)
    finally:
        await conn.close()
    if not chats:
        log.info("Новый лид %s, но никто не нажал /start у бота", lead.get("id"))
        return
    body = {
        "text": _format_lead(lead),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": _keyboard(lead),
    }
    for chat_id in chats:
        try:
            await _post("sendMessage", {"chat_id": chat_id, **body})
        except Exception:
            log.exception("Не удалось отправить лид %s в чат %s", lead.get("id"), chat_id)


async def _handle_start(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    username = (message.get("from") or {}).get("username")
    conn = await db.connect()
    try:
        await db.add_subscriber(conn, int(chat_id), username)
    finally:
        await conn.close()
    await _post(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "Готово. Сюда будут приходить новые заказы и вакансии.\n\n"
                "«Открыть в Telegram» — исходный пост.\n"
                "«Написать» — диалог с человеком, если у него есть юзернейм.\n"
                "«Откликнуться» пока помечает карточку.\n"
                "«Не моё» — спрошу, почему. Комментарий станет правилом для следующих заявок.\n"
                "По будням утром пришлю сводку за день.\n\n"
                "Вся лента по-прежнему на http://127.0.0.1:8787"
            ),
        },
    )


async def _take_lead(lead_id: int) -> None:
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return
        await db.set_status(conn, lead_id, "take")
    finally:
        await conn.close()
    from app.reply import start_reply

    await start_reply(lead_id)


async def _reply_lead(lead_id: int, chat_id: int | None) -> None:
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
    finally:
        await conn.close()
    if not lead:
        if chat_id:
            await _post("sendMessage", {"chat_id": chat_id, "text": "Не нашёл заявку"})
        return
    from app.reply import start_reply

    text = (lead.get("reply_text") or "").strip()
    state = lead.get("reply_state") or ""
    if state == "ready" and text and chat_id:
        await _post(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        return
    if state != "writing":
        await start_reply(lead_id)
    if chat_id:
        await _post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Пишу отклик. Нажми «Написать отклик» ещё раз через минуту — пришлю текст.",
            },
        )


async def _handle_callback(query: dict) -> None:
    data = query.get("data") or ""
    callback_id = query.get("id")
    if not callback_id:
        return
    if ":" not in data:
        await _answer_callback(callback_id, "Не понял кнопку")
        return
    action, raw_id = data.split(":", 1)
    try:
        lead_id = int(raw_id)
    except ValueError:
        await _answer_callback(callback_id, "Битая кнопка")
        return

    # Сначала отвечаем Telegram — иначе кнопка «зависает» при долгой работе с БД.
    if action == "a":
        await _answer_callback(
            callback_id,
            "Взял. Отклик пишется, кнопка «Написать отклик» покажет текст.",
        )
        asyncio.create_task(_take_lead(lead_id))
        return

    if action == "n":
        from_user = query.get("from") or {}
        chat = (query.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id") or from_user.get("id")
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
        finally:
            await conn.close()
        if chat_id and lead:
            _pending_reject[int(chat_id)] = lead_id
            await _answer_callback(callback_id, "Напиши, почему не твоё")
            await _post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Почему эта заявка тебе не подходит? Одной фразой — это станет правилом для следующих.",
                    "reply_markup": {"force_reply": True, "selective": True},
                },
            )
        else:
            await _answer_callback(callback_id, "Не нашёл заявку")
        return

    if action == "r":
        chat = (query.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id") or (query.get("from") or {}).get("id")
        await _answer_callback(callback_id, "Смотрю отклик…")
        asyncio.create_task(_reply_lead(lead_id, int(chat_id) if chat_id else None))
        return

    if action == "w":
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
        finally:
            await conn.close()
        if lead and lead.get("write_url"):
            await _answer_callback(callback_id, f"Пиши сюда: {lead['write_url']}", alert=True)
        else:
            await _answer_callback(
                callback_id,
                "У автора нет публичного юзернейма. Открой пост и напиши ему оттуда.",
                alert=True,
            )
        return

    await _answer_callback(callback_id, "Ок")


async def _handle_update(update: dict) -> None:
    if update.get("message"):
        message = update["message"]
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if text.startswith("/start"):
            if chat_id:
                _pending_reject.pop(int(chat_id), None)
            await _handle_start(message)
            return
        if chat_id and int(chat_id) in _pending_reject and text:
            lead_id = _pending_reject.pop(int(chat_id))
            conn = await db.connect()
            try:
                lead = await db.get_lead(conn, lead_id)
                await db.reject_lead(conn, lead_id, text)
            finally:
                await conn.close()
            row = lessons.add_lesson(text, lead_id, about=(lead or {}).get("text") or "")
            conclusion = (row or {}).get("conclusion") or f"Запомнил: «{text}»."
            instruction = (row or {}).get("instruction") or ""
            note = conclusion if not instruction else f"{conclusion}\n\nФиксик предлагает внести в инструкции: {instruction}"
            await _post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": note,
                },
            )
            return
        return
    if update.get("callback_query"):
        await _handle_callback(update["callback_query"])


async def _poll_loop() -> None:
    global _offset
    log.info("Бот уведомлений слушает обновления")
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.get(
                    _api("getUpdates"),
                    params={"timeout": 25, "offset": _offset},
                )
                payload = resp.json()
            if not payload.get("ok"):
                log.warning("getUpdates: %s", payload)
                await asyncio.sleep(3)
                continue
            for update in payload.get("result") or []:
                _offset = int(update["update_id"]) + 1
                try:
                    await _handle_update(update)
                except Exception:
                    log.exception("Ошибка обработки апдейта бота")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Сбой long poll бота")
            await asyncio.sleep(3)


def _moscow_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


async def _day_leads() -> list[dict]:
    start = (_moscow_now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (start - timedelta(hours=3)).isoformat(timespec="seconds")
    conn = await db.connect()
    try:
        cur = await conn.execute(
            """
            SELECT channel_username, fit, text, url
            FROM leads
            WHERE fit >= 3 AND status != 'reject' AND COALESCE(message_date, created_at) >= ?
            ORDER BY fit DESC, id DESC
            LIMIT 15
            """,
            (since,),
        )
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await conn.close()


def _digest_text(rows: list[dict]) -> str:
    day = (_moscow_now() - timedelta(days=1)).strftime("%d.%m")
    if not rows:
        return f"Сводка за {day}: живых заказов на ИИ не было."
    lines = [f"Сводка за {day}: {len(rows)} заказ(ов) на ИИ.", ""]
    for row in rows:
        title = re.sub(r"\s+", " ", (row.get("text") or "")).strip()
        if len(title) > 110:
            title = title[:110] + "…"
        channel = row.get("channel_username") or "чат"
        lines.append(f"• {row.get('fit')}/5 · @{channel}")
        lines.append(html.escape(title))
        if row.get("url"):
            lines.append(row["url"])
        lines.append("")
    lines.append("Лента: http://127.0.0.1:8787")
    return "\n".join(lines).strip()


async def send_morning_digest() -> None:
    if not enabled():
        return
    conn = await db.connect()
    try:
        chats = await db.list_subscribers(conn)
    finally:
        await conn.close()
    if not chats:
        return
    body = _digest_text(await _day_leads())
    for chat_id in chats:
        try:
            await _post("sendMessage", {"chat_id": chat_id, "text": body, "disable_web_page_preview": True})
        except Exception:
            log.exception("Не удалось отправить утреннюю сводку в %s", chat_id)


async def _digest_loop() -> None:
    mark = settings.db_path.parent / "digest-day.txt"
    while True:
        now = _moscow_now()
        today = now.date().isoformat()
        sent = mark.read_text(encoding="utf-8").strip() if mark.exists() else ""
        if 8 <= now.hour < 12 and sent != today:
            try:
                await send_morning_digest()
                mark.write_text(today, encoding="utf-8")
                log.info("Утренняя сводка отправлена")
            except Exception:
                log.exception("Утренняя сводка не ушла")
        await asyncio.sleep(600)


def running() -> bool:
    return _poll_task is not None and not _poll_task.done()


async def start() -> bool:
    global _poll_task, _digest_task
    if not enabled():
        log.warning("BOT_TOKEN пустой — уведомления в Telegram выключены")
        return False
    if running():
        return True
    try:
        me = await _post("getMe", {})
        log.info("Бот уведомлений: @%s", me.get("username"))
        try:
            await _post("deleteWebhook", {"drop_pending_updates": False})
        except Exception:
            log.info("Webhook у бота не снимался — ок, если его не было")
    except Exception:
        log.exception("Не удалось достучаться до Bot API. Проверь BOT_TOKEN.")
        return False
    _poll_task = asyncio.create_task(_poll_loop())
    if _digest_task is None or _digest_task.done():
        _digest_task = asyncio.create_task(_digest_loop())
    return True


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def stop() -> None:
    global _poll_task, _digest_task
    await _cancel(_poll_task)
    await _cancel(_digest_task)
    _poll_task = None
    _digest_task = None
