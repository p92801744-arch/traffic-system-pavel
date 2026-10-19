from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import db, lessons
from app.servicekit import atomic_json, heartbeat_fresh, local_site, read_heartbeat, read_json, runtime_dir
from app.settings import settings

log = logging.getLogger("leads.notifier")

_poll_task: asyncio.Task | None = None
_house_task: asyncio.Task | None = None
_offset = 0
_pending_reject: dict[int, int] = {}

QUIET_START_HOUR = 22
QUIET_END_HOUR = 8
CONFLICT_PAUSE_SECONDS = 30
CALLBACK_TEXT_LIMIT = 200
WHY_LIMIT = 180
EXCERPT_LIMIT = 500
FLUSH_LIMIT = 12
FLUSH_HOURS = 16
STALE_SECONDS = 180
WATCH_GRACE_SECONDS = 180
NETWORK_GRACE_SECONDS = 600

SOURCE_LABELS = {
    "telegram": "Telegram",
    "hh": "HeadHunter",
    "fl": "FL.ru",
    "kwork": "Kwork",
    "profi": "Профи.ру",
    "avito": "Авито",
    "weblancer": "Weblancer",
    "freelance_ru": "Freelance.ru",
}

WATCH_ROLES = (
    ("telegram", "Слушатель Telegram"),
    ("boards", "Сбор бирж"),
    ("networking", "Сбор встреч"),
)

BOT_COMMANDS = [
    {"command": "start", "description": "Включить уведомления"},
    {"command": "help", "description": "Что умеют кнопки"},
    {"command": "menu", "description": "Команды и кнопки"},
    {"command": "status", "description": "Живы ли сборщики"},
    {"command": "stats", "description": "Сколько заявок в ленте"},
    {"command": "cancel", "description": "Отменить причину «не моё»"},
]


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.bot_token}/{method}"


def _redact(text: str) -> str:
    token = (settings.bot_token or "").strip()
    if token:
        text = text.replace(token, "***")
    return text


def enabled() -> bool:
    return bool(settings.bot_token)


def _kind_label(kind: str | None) -> str:
    return "Вакансия" if kind == "vacancy" else "Заказ"


def _source_label(lead: dict) -> str:
    source = (lead.get("source") or "telegram").strip() or "telegram"
    label = SOURCE_LABELS.get(source, source)
    if source == "telegram":
        channel = (lead.get("channel_title") or lead.get("channel_username") or "").strip().lstrip("@")
        if channel:
            return f"Telegram, {channel}"
    return label


REPLY_LIMIT = 1800


def _short(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _format_lead(lead: dict) -> str:
    kind = _kind_label(lead.get("kind"))
    lines = [f"<b>{html.escape(kind)}</b>", f"Откуда: {html.escape(_source_label(lead))}"]
    sender = (lead.get("sender_name") or lead.get("sender_username") or "").strip()
    if sender:
        lines.append(f"Кто: {html.escape(sender)}")
    if lead.get("fit"):
        extra = ", высокий чек" if lead.get("high_ticket") else ""
        lines.append(f"Оценка: {lead['fit']}/5{extra}")
    why = _short(lead.get("why") or "", WHY_LIMIT)
    if why:
        lines.append("Почему: " + html.escape(why))
    excerpt = _short(lead.get("text") or "", EXCERPT_LIMIT)
    if excerpt:
        lines.append("")
        lines.append(html.escape(excerpt))
    draft = (lead.get("reply_text") or "").strip()
    if draft and (lead.get("source") or "") == "kwork":
        lines.append("")
        lines.append("<b>Отклик:</b>")
        lines.append(html.escape(_short(draft, REPLY_LIMIT)))
    url = (lead.get("url") or "").strip()
    if url:
        lines.append("")
        lines.append(f'<a href="{html.escape(url, quote=True)}">Открыть</a>')
    return "\n".join(lines)


def _keyboard(lead: dict) -> dict:
    lead_id = lead["id"]
    rows: list[list[dict]] = []
    url = (lead.get("url") or "").strip()
    if url:
        rows.append([{"text": "Открыть", "url": url}])
    if lead.get("write_url"):
        rows.append([{"text": "Написать", "url": lead["write_url"]}])
    elif lead.get("sender_username") or (lead.get("source") or "telegram") == "telegram":
        rows.append([{"text": "Написать", "callback_data": f"w:{lead_id}"}])
    rows.append([{"text": "Черновик", "callback_data": f"r:{lead_id}"}])
    rows.append(
        [
            {"text": "Не моё", "callback_data": f"n:{lead_id}"},
            {"text": "Позже", "callback_data": f"l:{lead_id}"},
            {"text": "Готово", "callback_data": f"d:{lead_id}"},
        ]
    )
    return {"inline_keyboard": rows}


def _menu_keyboard() -> dict:
    return {
        "keyboard": [["/status", "/stats"], ["/help"]],
        "resize_keyboard": True,
    }


def _help_text() -> str:
    return (
        "Сюда приходят новые заказы с оценкой от 3.\n\n"
        "В карточке: откуда заказ, оценка, короткая причина и ссылка.\n"
        "«Открыть» — исходный пост.\n"
        "«Написать» — диалог, если у человека есть юзернейм.\n"
        "«Черновик» — текст отклика. Если он ещё пишется, нажми ещё раз через минуту.\n"
        "«Не моё» — спрошу причину. Она станет правилом для следующих заявок.\n"
        "«Позже» — вернуть карточку в ленту на сайте.\n"
        "«Готово» — убрать из новых и оставить в разделе «Взятые».\n\n"
        "С 22:00 до 08:00 по Москве карточки не шлю. Утром пришлю накопившиеся и короткую сводку.\n"
        "Если сборщик замолчит или Telegram попросит войти заново, напишу сразу, в том числе ночью.\n"
        "/status — живы ли сборщики.\n"
        "/stats — сколько заявок в ленте.\n"
        "/cancel — отменить недописанную причину «не моё».\n\n"
        f"Лента: {local_site()}"
    )


def _callback_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= CALLBACK_TEXT_LIMIT:
        return clean
    return clean[: CALLBACK_TEXT_LIMIT - 1].rstrip() + "…"


def _command(text: str) -> str:
    if not text.startswith("/"):
        return ""
    head = text.split()[0]
    return head.split("@", 1)[0].lower()


def _moscow_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


def in_quiet_hours(moment: datetime | None = None) -> bool:
    now = moment or _moscow_now()
    return now.hour >= QUIET_START_HOUR or now.hour < QUIET_END_HOUR


def _poll_decision(status_code: int, payload: dict) -> str:
    description = str(payload.get("description") or "").lower()
    if status_code == 401 or "unauthorized" in description:
        return "stop"
    if status_code == 409 or "terminated by other getupdates" in description or "conflict" in description:
        return "conflict"
    if status_code != 200 or not payload.get("ok"):
        return "retry"
    return "ok"


def _offset_path():
    return runtime_dir() / "bot-offset.txt"


def _load_offset() -> int:
    path = _offset_path()
    if not path.exists():
        return 0
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def _save_offset(value: int) -> None:
    path = _offset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".txt.tmp")
    temporary.write_text(str(int(value)), encoding="utf-8")
    temporary.replace(path)


def _load_pending() -> None:
    global _pending_reject
    data = read_json(runtime_dir() / "bot-pending.json") or {}
    cleaned: dict[int, int] = {}
    for key, value in data.items():
        try:
            cleaned[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    _pending_reject = cleaned


def _save_pending() -> None:
    atomic_json(
        runtime_dir() / "bot-pending.json",
        {str(key): value for key, value in _pending_reject.items()},
    )


def _load_watch() -> dict:
    data = read_json(runtime_dir() / "bot-watch.json") or {}
    return data if isinstance(data, dict) else {}


def _save_watch(state: dict) -> None:
    atomic_json(runtime_dir() / "bot-watch.json", state)


def watch_problem(data: dict | None) -> str | None:
    """Пустой пульс значит, что процесс не под супервизором. Это не авария."""
    if not data:
        return None
    state = str(data.get("state") or "")
    if state == "idle":
        return None
    if state == "need_login":
        return "нужен вход в Telegram на сайте"
    if not heartbeat_fresh(data, STALE_SECONDS):
        return "процесс молчит"
    if state == "retry":
        return "перезапускается и не поднимается"
    if state == "waiting_network":
        return "нет сети"
    return None


def _grace_for(problem: str) -> float:
    if problem == "нет сети":
        return NETWORK_GRACE_SECONDS
    return WATCH_GRACE_SECONDS


def advance_watch(previous: dict, readings: dict[str, dict | None], *, now: float) -> tuple[dict, list[str]]:
    state: dict[str, dict] = {}
    messages: list[str] = []
    for role, title in WATCH_ROLES:
        problem = watch_problem(readings.get(role))
        prev = previous.get(role) if isinstance(previous.get(role), dict) else {}
        if not problem:
            if prev.get("alerted"):
                messages.append(f"{title} снова работает.")
            continue
        same = prev.get("problem") == problem and "since" in prev
        since = float(prev["since"]) if same else now
        alerted = bool(prev.get("alerted")) if same else False
        if not alerted and now - since >= _grace_for(problem):
            messages.append(f"{title}: {problem}. Журнал: apps/leads/logs.")
            alerted = True
        state[role] = {"problem": problem, "since": since, "alerted": alerted}
    return state, messages


def describe_heartbeat(role: str, data: dict | None) -> str:
    titles = {name: title for name, title in WATCH_ROLES}
    titles["notifier"] = "Бот"
    title = titles.get(role, role)
    if not data:
        return f"{title}: пульса нет"
    state = str(data.get("state") or "")
    detail = str(data.get("detail") or "").strip()
    if state == "need_login":
        line = f"{title}: нужен вход на сайте"
    elif state == "waiting_network":
        line = f"{title}: ждёт сеть"
    elif state == "idle":
        line = f"{title}: выключен"
    elif state == "retry":
        line = f"{title}: перезапускается"
    elif not heartbeat_fresh(data, STALE_SECONDS):
        line = f"{title}: молчит"
    elif state == "running":
        line = f"{title}: работает"
    else:
        line = f"{title}: {state or 'без статуса'}"
    if detail and state != "running":
        line = f"{line}. {detail}"
    return line


def _site_line() -> str:
    url = local_site()
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as response:
            ok = response.status == 200
    except Exception:
        ok = False
    if ok:
        return f"Сайт открывается: {url}"
    return f"Сайт не отвечает: {url}"


async def _post(method: str, payload: dict) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(_api(method), json=payload)
            data = resp.json()
    except Exception as exc:
        raise RuntimeError(_redact(str(exc))) from None
    if not isinstance(data, dict) or not data.get("ok"):
        description = ""
        if isinstance(data, dict):
            description = str(data.get("description") or "")
        raise RuntimeError(_redact(description or "ошибка Bot API"))
    result = data.get("result")
    return result if isinstance(result, dict) else {}


async def _answer_callback(callback_id: str, text: str, *, alert: bool = False) -> None:
    try:
        await _post(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": _callback_text(text),
                "show_alert": alert,
            },
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "too old" in msg or "query_id_invalid" in msg or "query is too old" in msg:
            log.info("Кнопка бота уже протухла")
            return
        log.error("Не ответил на кнопку: %s", _redact(str(exc)))


def _track(coro) -> None:
    task = asyncio.create_task(coro)

    def _done(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc:
            log.error("Фоновая задача бота: %s", _redact(str(exc)))

    task.add_done_callback(_done)


async def _subscribers() -> list[int]:
    conn = await db.connect()
    try:
        return await db.list_subscribers(conn)
    finally:
        await conn.close()


async def _broadcast(text: str) -> bool:
    if not enabled() or not text.strip():
        return False
    chats = await _subscribers()
    if not chats:
        return False
    delivered = False
    for chat_id in chats:
        try:
            await _post("sendMessage", {"chat_id": chat_id, "text": text})
            delivered = True
        except Exception as exc:
            log.error("Не отправил сообщение в %s: %s", chat_id, _redact(str(exc)))
    return delivered


async def send_lead(lead: dict, *, min_fit: int = 3) -> None:
    if not enabled():
        return
    if (lead.get("status") or "new") == "reject":
        return
    fit = lead.get("fit")
    if fit is None or int(fit) < min_fit:
        return
    if in_quiet_hours():
        log.info("Тихие часы, заявка %s уйдёт утром", lead.get("id"))
        return
    chats = await _subscribers()
    if not chats:
        log.info("Новая заявка %s, но никто не нажал /start у бота", lead.get("id"))
        return
    lead_id = lead.get("id")
    claimed = False
    if lead_id:
        conn = await db.connect()
        try:
            claimed = await db.claim_bot_send(conn, int(lead_id))
        finally:
            await conn.close()
        if not claimed:
            return
    delivered = False
    try:
        body = {
            "text": _format_lead(lead),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _keyboard(lead),
        }
        for chat_id in chats:
            try:
                await _post("sendMessage", {"chat_id": chat_id, **body})
                delivered = True
            except Exception as exc:
                log.error(
                    "Не удалось отправить заявку %s в чат %s: %s",
                    lead.get("id"),
                    chat_id,
                    _redact(str(exc)),
                )
    finally:
        if lead_id and claimed and not delivered:
            conn = await db.connect()
            try:
                await db.release_bot_send(conn, int(lead_id))
            finally:
                await conn.close()


async def flush_unsent_cards(*, limit: int = FLUSH_LIMIT, hours: int = FLUSH_HOURS) -> int:
    if not enabled() or in_quiet_hours():
        return 0
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = await db.connect()
    try:
        rows = await db.unsent_bot_leads(conn, since, limit=limit)
    finally:
        await conn.close()
    sent = 0
    for row in rows:
        await send_lead(row)
        sent += 1
    return sent


async def _send_text(chat_id: int, text: str, *, menu: bool = False) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if menu:
        payload["reply_markup"] = _menu_keyboard()
    await _post("sendMessage", payload)


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
    await _send_text(int(chat_id), "Готово. " + _help_text(), menu=True)


async def _status_text() -> str:
    lines = [describe_heartbeat(role, read_heartbeat(role)) for role, _title in WATCH_ROLES]
    lines.append(describe_heartbeat("notifier", read_heartbeat("notifier")))
    lines.append(await asyncio.to_thread(_site_line))
    return "\n".join(lines)


async def _stats_text() -> str:
    conn = await db.connect()
    try:
        row = await db.stats(conn)
    finally:
        await conn.close()
    return (
        "Лента\n"
        f"Всего: {row['total']}\n"
        f"Новые: {row['new_count']}\n"
        f"В работе: {row['take_count']}\n"
        f"Готовые: {row['contacted_count']}\n"
        f"Высокий чек: {row['high_ticket_count']}\n"
        f"Сайт: {local_site()}"
    )


async def _take_lead(lead_id: int, chat_id: int | None) -> None:
    try:
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
            if not lead:
                lead = None
            else:
                await db.set_status(conn, lead_id, "take")
        finally:
            await conn.close()
        if not lead:
            if chat_id:
                await _send_text(chat_id, "Не нашёл заявку")
            return
        from app.reply import auto_reply_allowed, start_reply

        if auto_reply_allowed(lead):
            await start_reply(lead_id)
    except Exception as exc:
        log.error("Не взял заявку %s: %s", lead_id, _redact(str(exc)))
        if chat_id:
            await _send_text(chat_id, "Не смог взять заявку. Открой её на сайте.")


async def _reply_lead(lead_id: int, chat_id: int | None) -> None:
    try:
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
        finally:
            await conn.close()
        if not lead:
            if chat_id:
                await _send_text(chat_id, "Не нашёл заявку")
            return
        from app.reply import start_reply

        text = (lead.get("reply_text") or "").strip()
        state = lead.get("reply_state") or ""
        if state == "ready" and text and chat_id:
            await _send_text(chat_id, text)
            return
        if state != "writing":
            await start_reply(lead_id)
        if chat_id:
            await _send_text(chat_id, "Пишу черновик. Нажми «Черновик» ещё раз через минуту — пришлю текст.")
    except Exception as exc:
        log.error("Не собрал черновик %s: %s", lead_id, _redact(str(exc)))
        if chat_id:
            await _send_text(chat_id, "Черновик не собрался. Открой заявку на сайте и попроси написать ещё раз.")


async def _set_lead_status(lead_id: int, status: str) -> bool | None:
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return None
        await db.set_status(conn, lead_id, status)
        return True
    finally:
        await conn.close()


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
    chat = (query.get("message") or {}).get("chat") or {}
    chat_id = chat.get("id") or (query.get("from") or {}).get("id")
    chat_id = int(chat_id) if chat_id else None

    if action in ("a", "r", "w", "n", "l", "d"):
        await _dispatch_callback(action, lead_id, callback_id, chat_id)
        return
    await _answer_callback(callback_id, "Ок")


async def _dispatch_callback(action: str, lead_id: int, callback_id: str, chat_id: int | None) -> None:
    if action == "a":
        await _answer_callback(callback_id, "Взял. Черновик пишется, кнопка «Черновик» покажет текст.")
        _track(_take_lead(lead_id, chat_id))
        return

    if action == "r":
        await _answer_callback(callback_id, "Смотрю черновик")
        _track(_reply_lead(lead_id, chat_id))
        return

    if action == "l":
        try:
            updated = await _set_lead_status(lead_id, "new")
        except Exception as exc:
            log.error("Не вернул заявку %s: %s", lead_id, _redact(str(exc)))
            await _answer_callback(callback_id, "Не получилось, попробуй ещё раз")
            return
        await _answer_callback(callback_id, "Не нашёл заявку" if updated is None else "Вернул в ленту")
        return

    if action == "d":
        try:
            updated = await _set_lead_status(lead_id, "contacted")
        except Exception as exc:
            log.error("Не закрыл заявку %s: %s", lead_id, _redact(str(exc)))
            await _answer_callback(callback_id, "Не получилось, попробуй ещё раз")
            return
        await _answer_callback(callback_id, "Не нашёл заявку" if updated is None else "Отметил готовым")
        return

    if action == "n":
        try:
            conn = await db.connect()
            try:
                lead = await db.get_lead(conn, lead_id)
            finally:
                await conn.close()
        except Exception as exc:
            log.error("Не открыл заявку %s: %s", lead_id, _redact(str(exc)))
            await _answer_callback(callback_id, "Не получилось, попробуй ещё раз")
            return
        if chat_id and lead:
            _pending_reject[chat_id] = lead_id
            _save_pending()
            await _answer_callback(callback_id, "Напиши, почему не твоё")
            try:
                await _post(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "Почему эта заявка не подходит? Одной фразой. Это станет правилом для следующих.",
                        "reply_markup": {"force_reply": True, "selective": True},
                    },
                )
            except Exception as exc:
                log.error("Не спросил причину: %s", _redact(str(exc)))
        else:
            await _answer_callback(callback_id, "Не нашёл заявку")
        return

    if action == "w":
        try:
            conn = await db.connect()
            try:
                lead = await db.get_lead(conn, lead_id)
            finally:
                await conn.close()
        except Exception as exc:
            log.error("Не открыл контакт %s: %s", lead_id, _redact(str(exc)))
            await _answer_callback(callback_id, "Не получилось, попробуй ещё раз")
            return
        if lead and lead.get("write_url"):
            await _answer_callback(callback_id, f"Пиши сюда: {lead['write_url']}", alert=True)
        else:
            await _answer_callback(
                callback_id,
                "У автора нет публичного юзернейма. Открой пост и напиши ему оттуда.",
                alert=True,
            )


async def _remember_reason(chat_id: int, lead_id: int, text: str) -> None:
    try:
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
            already = bool(lead) and lead.get("status") == "reject" and (lead.get("reject_reason") or "") == text
            if lead and not already:
                await db.reject_lead(conn, lead_id, text)
        finally:
            await conn.close()
        if not lead:
            await _send_text(chat_id, "Не нашёл заявку.")
        elif already:
            await _send_text(chat_id, f"Уже запомнил: «{text}».")
        else:
            row = lessons.add_lesson(text, lead_id, about=(lead or {}).get("text") or "")
            conclusion = (row or {}).get("conclusion") or f"Запомнил: «{text}»."
            instruction = (row or {}).get("instruction") or ""
            note = conclusion if not instruction else f"{conclusion}\n\nФиксик предлагает внести в инструкции: {instruction}"
            await _send_text(chat_id, note)
    except Exception as exc:
        log.error("Не записал причину: %s", _redact(str(exc)))
        try:
            await _send_text(chat_id, "Не записал причину. Пришли её ещё раз.")
        except Exception:
            log.error("Не смог попросить причину ещё раз")
        return
    _pending_reject.pop(chat_id, None)
    _save_pending()


async def _handle_command(command: str, message: dict, chat_id: int | None) -> None:
    if command == "/start":
        if chat_id:
            _pending_reject.pop(chat_id, None)
            _save_pending()
        await _handle_start(message)
        return
    if not chat_id:
        return
    if command == "/cancel":
        if _pending_reject.pop(chat_id, None) is None:
            await _send_text(chat_id, "Нечего отменять.")
        else:
            _save_pending()
            await _send_text(chat_id, "Отменил. Заявку не трогал.")
        return
    if command in ("/help", "/menu"):
        await _send_text(chat_id, _help_text(), menu=True)
        return
    if command == "/status":
        await _send_text(chat_id, await _status_text())
        return
    if command == "/stats":
        await _send_text(chat_id, await _stats_text())
        return
    await _send_text(chat_id, "Такой команды нет. " + _help_text(), menu=True)


async def _handle_update(update: dict) -> None:
    if update.get("message"):
        message = update["message"]
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        chat_id = int(chat_id) if chat_id else None
        command = _command(text)
        if command:
            await _handle_command(command, message, chat_id)
            return
        if chat_id and chat_id in _pending_reject and text:
            await _remember_reason(chat_id, _pending_reject[chat_id], text)
        return
    if update.get("callback_query"):
        await _handle_callback(update["callback_query"])


async def _consume_update(update: dict) -> None:
    global _offset
    try:
        await _handle_update(update)
    except Exception as exc:
        log.error("Ошибка обработки апдейта бота: %s", _redact(str(exc)))
    finally:
        try:
            _offset = int(update["update_id"]) + 1
        except (KeyError, TypeError, ValueError):
            log.error("Битый update_id, пропускаю")
            return
        _save_offset(_offset)


async def _poll_loop() -> None:
    global _offset
    _offset = _load_offset()
    _load_pending()
    log.info("Бот уведомлений слушает обновления")
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.get(
                    _api("getUpdates"),
                    params={"timeout": 25, "offset": _offset},
                )
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            decision = _poll_decision(resp.status_code, payload)
            if decision == "stop":
                log.error("BOT_TOKEN отклонён, останавливаю опрос")
                return
            if decision == "conflict":
                log.warning("Другой процесс уже слушает этого бота, жду и повторю")
                await asyncio.sleep(CONFLICT_PAUSE_SECONDS)
                continue
            if decision != "ok":
                log.warning("getUpdates: %s", _redact(str(payload)[:300]))
                await asyncio.sleep(3)
                continue
            for update in payload.get("result") or []:
                await _consume_update(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Сбой long poll бота: %s", _redact(str(exc)))
            await asyncio.sleep(3)


async def _day_leads() -> list[dict]:
    start = (_moscow_now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (start - timedelta(hours=3)).isoformat(timespec="seconds")
    conn = await db.connect()
    try:
        cur = await conn.execute(
            """
            SELECT source, channel_username, channel_title, fit, text, url
            FROM leads
            WHERE fit >= 3 AND status IN ('new', 'take', 'contacted')
              AND COALESCE(message_date, created_at) >= ?
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
        return f"Сводка за {day}: подходящих заявок не было."
    lines = [f"Сводка за {day}: {len(rows)}.", ""]
    for row in rows:
        title = _short(row.get("text") or "", 110)
        lines.append(f"{row.get('fit')}/5 · {_source_label(row)}")
        if title:
            lines.append(title)
        if row.get("url"):
            lines.append(row["url"])
        lines.append("")
    lines.append(f"Лента: {local_site()}")
    return "\n".join(lines).strip()


def _read_text(path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def send_morning_digest() -> None:
    if not enabled():
        return
    chats = await _subscribers()
    if not chats:
        return
    body = _digest_text(await _day_leads())
    for chat_id in chats:
        try:
            await _post("sendMessage", {"chat_id": chat_id, "text": body, "disable_web_page_preview": True})
        except Exception as exc:
            log.error("Не удалось отправить утреннюю сводку в %s: %s", chat_id, _redact(str(exc)))


async def run_watch() -> None:
    previous = _load_watch()
    readings = {role: read_heartbeat(role) for role, _title in WATCH_ROLES}
    state, messages = advance_watch(previous, readings, now=time.monotonic())
    if messages and not await _broadcast("\n".join(messages)):
        for role, item in list(state.items()):
            prev = previous.get(role) if isinstance(previous.get(role), dict) else {}
            if item.get("alerted") and not prev.get("alerted"):
                item["alerted"] = False
        for role, _title in WATCH_ROLES:
            prev = previous.get(role) if isinstance(previous.get(role), dict) else None
            if prev and prev.get("alerted") and role not in state and watch_problem(readings.get(role)) is None:
                state[role] = prev
    _save_watch(state)


async def _housekeeping_loop() -> None:
    mark = settings.db_path.parent / "digest-day.txt"
    while True:
        if not in_quiet_hours():
            try:
                await flush_unsent_cards()
            except Exception as exc:
                log.error("Не отправил накопленные карточки: %s", _redact(str(exc)))
        now = _moscow_now()
        today = now.date().isoformat()
        if 8 <= now.hour < 12 and _read_text(mark) != today:
            try:
                await send_morning_digest()
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text(today, encoding="utf-8")
                log.info("Утренняя сводка отправлена")
            except Exception as exc:
                log.error("Утренняя сводка не ушла: %s", _redact(str(exc)))
        try:
            await run_watch()
        except Exception as exc:
            log.error("Проверка сборщиков не вышла: %s", _redact(str(exc)))
        await asyncio.sleep(60)


def running() -> bool:
    return _poll_task is not None and not _poll_task.done()


async def start() -> bool:
    global _poll_task, _house_task
    if not enabled():
        log.warning("BOT_TOKEN пустой — уведомления в Telegram выключены")
        return False
    if running():
        return True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        me = await _post("getMe", {})
        log.info("Бот уведомлений: @%s", me.get("username"))
        try:
            await _post("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            log.info("Webhook у бота не снимался: %s", _redact(str(exc)))
        try:
            await _post("setMyCommands", {"commands": BOT_COMMANDS})
        except Exception as exc:
            log.info("Меню команд не обновилось: %s", _redact(str(exc)))
    except Exception as exc:
        log.error("Не удалось достучаться до Bot API: %s", _redact(str(exc)))
        return False
    _load_pending()
    _poll_task = asyncio.create_task(_poll_loop())
    if _house_task is None or _house_task.done():
        _house_task = asyncio.create_task(_housekeeping_loop())
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
    global _poll_task, _house_task
    await _cancel(_poll_task)
    await _cancel(_house_task)
    _poll_task = None
    _house_task = None
