from __future__ import annotations

import json
import logging
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import boards, chat_rating, chat_scan, collector, db, lessons, mailings, networking, notifier, scorer
from app.filters import candidate_channels, configured_channels, intake_hits, load_sources
from app import reply, updater
from app.servicekit import current_role, embed_workers
from app.settings import ROOT, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("leads")

templates = Jinja2Templates(directory=str(ROOT / "templates"))
_login_hash: dict[str, str] = {}


def feed_href(*, status: str = "", channel: str = "", show_low: bool = False) -> str:
    parts: list[str] = []
    if status:
        parts.append(f"status={status}")
    if channel:
        parts.append(f"channel={channel}")
    if show_low:
        parts.append("show_low=1")
    return ("/?" + "&".join(parts)) if parts else "/"


templates.env.globals["feed_href"] = feed_href


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await db.connect()
    await db.reset_writing_replies(conn)
    await conn.close()
    asyncio.create_task(_rescore_safe())
    if embed_workers():
        asyncio.create_task(boards.poll_forever())
        asyncio.create_task(networking.poll_forever())
        asyncio.create_task(_telegram_safe())
        await notifier.start()
    else:
        log.info("Сборщики и бот запущены отдельными процессами")
    yield
    if embed_workers():
        await notifier.stop()
    await collector.disconnect()


async def _telegram_safe() -> None:
    try:
        if await collector.is_authorized():
            await collector.start_listener()
            await _collect_history_safe()
    except Exception:
        log.exception("Telegram ещё не готов")


async def _rescore_safe():
    await _purge_safe()
    conn = await db.connect()
    try:
        n = 0
        for lead in await db.leads_for_rescore(conn):
            score = await scorer.score_lead(lead["text"], lead.get("channel_username"))
            await db.save_score(conn, lead["id"], score, commit=False)
            n += 1
        await conn.commit()
        log.info("Оценщик прошёл %s заявок", n)
    except Exception:
        log.exception("Не удалось переоценить заявки")
    finally:
        await conn.close()


async def _purge_safe():
    sources = load_sources()
    conn = await db.connect()
    try:
        junk = [
            lead["id"]
            for lead in await db.leads_for_purge(conn)
            if intake_hits(lead.get("text") or "", sources) is None
        ]
        copies = await db.duplicate_lead_ids(conn)
        drop = list(dict.fromkeys([*junk, *copies]))
        await db.delete_leads(conn, drop)
        if drop:
            log.info("Убрал из ленты %s сообщений: реклама, шлак или повтор текста", len(drop))
    except Exception:
        log.exception("Не удалось почистить ленту")
    finally:
        await conn.close()


async def _collect_history_safe():
    try:
        result = await collector.collect_history()
        log.info("Первый сбор: %s", result)
    except Exception:
        log.exception("Не удалось собрать историю при старте")


app = FastAPI(title="Ящик лидов", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def _decode_keywords(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


FEED_STATUSES = {"new", "take", "contacted", "reject"}


@app.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    status: str | None = None,
    channel: str | None = None,
    show_low: int | None = None,
    fresh: int | None = None,
    note: str | None = None,
):
    authorized = False
    try:
        authorized = await collector.is_authorized()
    except Exception:
        authorized = False

    if not settings.has_telegram_api or not authorized:
        return render(
            request,
            "setup.html",
            has_api=settings.has_telegram_api,
            phone=settings.telegram_phone,
            need_password=False,
            error=None,
            step="api" if not settings.has_telegram_api else "code",
        )

    conn = await db.connect()
    try:
        if fresh:
            leads = await db.list_leads(conn, unseen=True, channel=channel or None, hide_low=not show_low, limit=200)
        else:
            leads = await db.list_leads(
                conn,
                status=status if status in FEED_STATUSES else "new",
                channel=channel or None,
                hide_low=(show_low == 0) if show_low is not None else False,
            )
        unseen_count = await db.count_unseen(conn)
        for lead in leads:
            lead["keyword_list"] = _decode_keywords(lead.get("keywords"))
        counts = await db.stats(conn)
        db_channels = await db.channels(conn)
    finally:
        await conn.close()

    seen = set()
    channel_items = []
    for item in configured_channels():
        key = item["username"].lower()
        if key in seen:
            continue
        seen.add(key)
        channel_items.append(item)
    for name in db_channels:
        if name and name.lower() not in seen:
            seen.add(name.lower())
            channel_items.append({"username": name, "title": name})

    return render(
        request,
        "index.html",
        leads=leads,
        counts=counts,
        channels=channel_items,
        status=status if status in FEED_STATUSES else "",
        channel=channel or "",
        page="feed",
        scorer_on=True,
        fresh=bool(fresh),
        unseen_count=unseen_count,
        note=(note or "")[:600],
    )


@app.get("/taken", response_class=HTMLResponse)
async def taken(request: Request):
    conn = await db.connect()
    try:
        rows = await db.list_leads(conn, statuses=["take", "contacted"], hide_low=False, limit=80)
        for lead in rows:
            lead["keyword_list"] = _decode_keywords(lead.get("keywords"))
    finally:
        await conn.close()
    return render(request, "taken.html", leads=rows, page="taken")


@app.get("/rejects", response_class=HTMLResponse)
async def rejects(request: Request):
    conn = await db.connect()
    try:
        rows = await db.list_leads(conn, status="reject", hide_low=False, limit=40)
    finally:
        await conn.close()
    notes = lessons.records(30)
    for row in notes:
        if not row.get("instruction"):
            row["instruction"] = lessons.proposed_instruction(row.get("reason") or "")
    return render(
        request,
        "rejects.html",
        lessons=notes,
        leads=rows,
        page="rejects",
    )


@app.get("/networking", response_class=HTMLResponse)
async def networking_page(request: Request):
    events = await networking.upcoming()
    return render(
        request,
        "networking.html",
        events=events,
        chats=candidate_channels(),
        page="networking",
    )


def _safe_next(value: str | None, default: str = "/") -> str:
    """Только относительный путь внутри панели: начинается с '/', но не с '//' или '/\\'."""
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


def _safe_chats_next(value: str) -> str:
    value = (value or "").strip()
    if value == "/chats" or value.startswith(("/chats?", "/chats/")):
        return _safe_next(value, "/chats")
    return "/chats"


def _lead_missing() -> HTMLResponse:
    return HTMLResponse("Заявка не найдена", status_code=404)


def _chat_view(row: dict) -> dict:
    rules = chat_rating.rules_from_row(row)
    status = chat_rating.effective_status(row)
    try:
        tgstat = json.loads(row.get("tgstat_json") or "")
    except json.JSONDecodeError:
        tgstat = {}
    if not isinstance(tgstat, dict):
        tgstat = {}
    return {
        **row,
        "status": status,
        "status_label": chat_rating.STATUS_LABELS.get(status, status),
        "reason": chat_rating.display_reason(row),
        "rules_line": chat_rating.rules_summary(rules),
        "spam_pct": round(float(row.get("spam_score") or 0) * 100),
        "next_label": (row.get("next_card_at") or "").replace("T", " ")[:16],
        "tg_participants": tgstat.get("participants"),
    }


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, sort: str = "rating", dir: str = "desc"):
    if sort not in db.CHAT_SORTS:
        sort = "rating"
    direction = "asc" if dir == "asc" else "desc"
    conn = await db.connect()
    try:
        rows = [_chat_view(row) for row in await db.list_chat_profiles(conn, sort, direction)]
    finally:
        await conn.close()
    return render(
        request,
        "chats.html",
        chats=rows,
        sort=sort,
        direction=direction,
        page="chats",
        statuses=chat_rating.STATUSES,
        status_labels=chat_rating.STATUS_LABELS,
    )


@app.post("/chats/{chat_key}/status")
async def chat_status(chat_key: str, status: str = Form(""), next: str = Form("/chats")):
    if status not in {"", *chat_rating.STATUSES}:
        return RedirectResponse("/chats", status_code=303)
    conn = await db.connect()
    try:
        current = await db.get_chat_profile(conn, chat_key)
        if current:
            await db.set_chat_override(conn, chat_key, status or None)
            await chat_rating.refresh_card_plan(conn)
    finally:
        await conn.close()
    return RedirectResponse(_safe_chats_next(next), status_code=303)


@app.post("/chats/{chat_key}/draft")
async def chat_draft(chat_key: str, next: str = Form("/chats")):
    conn = await db.connect()
    try:
        row = await db.get_chat_profile(conn, chat_key)
        if row:
            draft = chat_rating.draft_card(row.get("title") or "", chat_rating.rules_from_row(row))
            await conn.execute(
                "UPDATE chat_profiles SET card_draft = ?, card_draft_note = ? WHERE chat_key = ?",
                (draft["text"], draft["note"], chat_key),
            )
            await conn.commit()
    finally:
        await conn.close()
    return RedirectResponse(_safe_chats_next(next), status_code=303)


def _localhost(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


@app.post("/internal/chat-rate")
async def internal_chat_rate(
    request: Request,
    limit: int = 0,
    only: str = "",
    force: int = 0,
    max_messages: int = 500,
):
    if not _localhost(request):
        return HTMLResponse("только с этого компьютера", status_code=403)
    if chat_scan.JOB.get("running"):
        return {"ok": True, "started": False, "reason": "уже идёт"}
    # Сразу помечаем прогон, иначе опрос успевает увидеть прошлый finished_at.
    chat_scan.JOB.update(
        running=True,
        done=0,
        total=0,
        current="",
        error="",
        finished_at=None,
        summary=None,
    )
    asyncio.create_task(
        _run_chat_rate(limit=limit, only=only, force=bool(force), max_messages=max_messages)
    )
    return {"ok": True, "started": True}


@app.get("/internal/chat-rate")
async def internal_chat_rate_status(request: Request):
    if not _localhost(request):
        return HTMLResponse("только с этого компьютера", status_code=403)
    job = chat_scan.JOB
    return {
        "ok": True,
        "running": bool(job.get("running")),
        "done": job.get("done") or 0,
        "total": job.get("total") or 0,
        "current": job.get("current") or "",
        "error": job.get("error") or "",
        "finished_at": job.get("finished_at"),
        "summary": job.get("summary"),
    }


async def _run_chat_rate(**kwargs) -> None:
    try:
        await chat_scan.run_guarded(**kwargs)
    except Exception as exc:
        log.exception("Прогон рейтинга чатов")
        chat_scan.JOB["error"] = str(exc)
        chat_scan.JOB["running"] = False
        if not chat_scan.JOB.get("finished_at"):
            from datetime import datetime, timezone

            chat_scan.JOB["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/mailings", response_class=HTMLResponse)
async def mailings_page(request: Request):
    return render(request, "mailings.html", drafts=mailings.load()["drafts"], page="mailings", saved=False)


@app.post("/mailings")
async def mailings_save(
    chat: str = Form(""),
    theme: str = Form(""),
    text: str = Form(""),
):
    if chat.strip() and text.strip():
        mailings.save_draft(chat, theme, text)
    return RedirectResponse("/mailings", status_code=303)


@app.post("/mailings/{index}/delete")
async def mailings_delete(index: int):
    mailings.delete_draft(index)
    return RedirectResponse("/mailings", status_code=303)


@app.get("/leads/{lead_id}/reply", response_class=HTMLResponse)
async def reply_page(request: Request, lead_id: int):
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return RedirectResponse("/", status_code=303)
        await db.mark_seen(conn, lead_id)
        state = lead.get("reply_state") or ""
        if state not in ("writing", "ready", "error") and reply.auto_reply_allowed(lead):
            await reply.start_reply(lead_id)
            lead = await db.get_lead(conn, lead_id) or lead
    finally:
        await conn.close()
    profile = {"about": "", "kind": ""}
    if (lead.get("reply_state") or "") != "writing":
        profile = await collector.author_profile(lead)
    return render(
        request,
        "reply.html",
        lead=lead,
        about=profile["about"],
        about_kind=profile["kind"],
        draft=(lead.get("reply_text") or "").strip(),
        reply_state=lead.get("reply_state") or "",
        reply_error=lead.get("reply_error") or "",
        page="",
    )


@app.post("/leads/{lead_id}/reply")
async def reply_revise(lead_id: int, instruction: str = Form(""), again: str = Form("")):
    conn = await db.connect()
    try:
        exists = await db.get_lead(conn, lead_id)
    finally:
        await conn.close()
    if not exists:
        return _lead_missing()
    text = instruction.strip()
    if not text and again != "1":
        return RedirectResponse(f"/leads/{lead_id}/reply", status_code=303)
    await reply.finish_reply(lead_id, text)
    return RedirectResponse(f"/leads/{lead_id}/reply", status_code=303)


@app.post("/setup/code")
async def setup_code(request: Request, phone: str = Form(...)):
    phone = phone.strip()
    ctx = {
        "has_api": True,
        "phone": phone,
        "need_password": False,
        "error": None,
        "step": "code",
    }
    try:
        phone_hash = await collector.send_login_code(phone)
        _login_hash[phone] = phone_hash
        ctx["step"] = "confirm"
        return render(request, "setup.html", **ctx)
    except Exception as exc:
        ctx["error"] = str(exc)
        return render(request, "setup.html", **ctx)


@app.post("/setup/sign-in")
async def setup_sign_in(
    request: Request,
    phone: str = Form(...),
    code: str = Form(...),
    password: str = Form(""),
):
    from telethon.errors import SessionPasswordNeededError

    phone = phone.strip()
    phone_hash = _login_hash.get(phone, "")
    try:
        await collector.sign_in(phone, code.strip(), phone_hash, password.strip() or None)
        if embed_workers():
            await collector.start_listener()
            try:
                await collector.collect_history()
            except Exception:
                log.exception("Сбор после входа не удался")
        else:
            log.info("Вход сохранён. Живую ленту подхватит процесс Telegram.")
        return RedirectResponse("/", status_code=303)
    except SessionPasswordNeededError:
        return render(
            request,
            "setup.html",
            has_api=True,
            phone=phone,
            need_password=True,
            error=None,
            step="confirm",
        )
    except Exception as exc:
        return render(
            request,
            "setup.html",
            has_api=True,
            phone=phone,
            need_password=False,
            error=str(exc),
            step="confirm",
        )


@app.post("/leads/{lead_id}/status")
async def update_status(lead_id: int, status: str = Form(...), next: str = Form("/")):
    allowed = {"new", "take", "contacted"}
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return _lead_missing()
        if status not in allowed:
            return RedirectResponse("/", status_code=303)
        await db.set_status(conn, lead_id, status)
    finally:
        await conn.close()
    if status == "take" and reply.auto_reply_allowed(lead):
        await reply.start_reply(lead_id)
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/leads/{lead_id}/reject")
async def reject_with_reason(
    lead_id: int,
    reason: str = Form(""),
    comment: str = Form(""),
    next: str = Form("/"),
):
    text = (reason or comment).strip()
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return _lead_missing()
        if not text:
            return RedirectResponse(_safe_next(next), status_code=303)
        await db.reject_lead(conn, lead_id, text)
        await db.mark_seen(conn, lead_id)
    finally:
        await conn.close()
    lessons.add_lesson(text, lead_id, about=(lead or {}).get("text") or "")
    return RedirectResponse(_safe_next(next), status_code=303)


_collect_lock = asyncio.Lock()


async def _collect_manual() -> None:
    if _collect_lock.locked():
        log.info("Сбор уже идёт — повторный запуск пропускаю")
        return
    async with _collect_lock:
        try:
            n = await boards.poll_once()
            log.info("Ручной сбор с бирж: %s", n)
        except Exception:
            log.exception("Ручной сбор с бирж не удался")
        try:
            await collector.refresh_watch_list()
            result = await collector.collect_history()
            log.info("Ручной сбор Telegram: %s", result)
        except Exception:
            log.exception("Ручной сбор Telegram не удался")


@app.get("/leads/{lead_id}/open")
async def open_lead(lead_id: int, to: str = "url"):
    """Отметить, что Павел открыл заявку, и увести на неё."""
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        if not lead:
            return _lead_missing()
        await db.mark_seen(conn, lead_id)
    finally:
        await conn.close()
    target = (lead.get("write_url") if to == "write" else lead.get("url")) or ""
    if not target.startswith(("https://", "http://", "tg://")):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(target, status_code=303)


@app.post("/leads/seen-all")
async def mark_all_seen():
    conn = await db.connect()
    try:
        await conn.execute("UPDATE leads SET seen_at = datetime('now') WHERE status = 'new' AND seen_at IS NULL")
        await conn.commit()
    finally:
        await conn.close()
    return RedirectResponse("/?fresh=1", status_code=303)


@app.post("/update")
async def update_inbox(request: Request):
    """«Обновить ящик»: обновление кода при наличии, затем сбор и лента непросмотренных."""
    info = await asyncio.to_thread(updater.check)
    if info["state"] == "ahead":
        pulled = await asyncio.to_thread(updater.pull)
        if pulled["ok"]:
            updater.spawn_restart(pulled["requirements"])
            return render(request, "updating.html", message=pulled["message"], restarting=True, page="")
        asyncio.create_task(_collect_manual())
        return RedirectResponse("/?fresh=1&note=" + quote(pulled["message"]), status_code=303)
    asyncio.create_task(_collect_manual())
    note = "Код свежий, собираю заявки." if info["state"] == "none" else info["message"]
    return render(request, "updating.html", message=note, restarting=False, page="")


@app.post("/collect")
async def collect_now():
    # Сразу отдаём страницу: долгий сбор Telegram/бирж не должен блокировать ленту и бота.
    asyncio.create_task(_collect_manual())
    return RedirectResponse("/", status_code=303)


@app.post("/leads/{lead_id}/delete")
async def delete_lead(lead_id: int, next: str = Form("/taken")):
    conn = await db.connect()
    try:
        if not await db.get_lead(conn, lead_id):
            return _lead_missing()
        await db.delete_leads(conn, [lead_id], remember=True)
    finally:
        await conn.close()
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/taken/clear")
async def clear_taken():
    conn = await db.connect()
    try:
        n = await db.delete_taken(conn)
        log.info("Очистил взятые: %s", n)
    finally:
        await conn.close()
    return RedirectResponse("/taken", status_code=303)


@app.get("/health")
async def health():
    authorized = False
    try:
        authorized = await collector.is_authorized()
    except Exception:
        pass
    from app import jev

    return {
        "ok": True,
        "role": current_role(),
        "telegram_api": settings.has_telegram_api,
        "authorized": authorized,
        "scorer": True,
        "jev": jev.enabled(),
        "bot": bool(settings.bot_token),
        "chat_rate": True,
    }
