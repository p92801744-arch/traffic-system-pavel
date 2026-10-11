from __future__ import annotations

import json
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import boards, collector, db, lessons, mailings, networking, notifier, scorer
from app.filters import candidate_channels, configured_channels, intake_hits, load_sources
from app import reply
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


@app.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    status: str | None = None,
    channel: str | None = None,
    show_low: int = 0,
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
        leads = await db.list_leads(
            conn,
            status="new",
            channel=channel or None,
            hide_low=False,
        )
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
        status="",
        channel=channel or "",
        page="feed",
        scorer_on=True,
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
        state = lead.get("reply_state") or ""
        if state not in ("writing", "ready", "error"):
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
    if status not in allowed:
        return RedirectResponse("/", status_code=303)
    conn = await db.connect()
    try:
        await db.set_status(conn, lead_id, status)
    finally:
        await conn.close()
    if status == "take":
        await reply.start_reply(lead_id)
    return RedirectResponse(next or "/", status_code=303)


@app.post("/leads/{lead_id}/reject")
async def reject_with_reason(
    lead_id: int,
    reason: str = Form(""),
    comment: str = Form(""),
    next: str = Form("/"),
):
    text = (reason or comment).strip()
    if not text:
        return RedirectResponse(next or "/", status_code=303)
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
        await db.reject_lead(conn, lead_id, text)
    finally:
        await conn.close()
    lessons.add_lesson(text, lead_id, about=(lead or {}).get("text") or "")
    return RedirectResponse(next or "/", status_code=303)


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


@app.post("/collect")
async def collect_now():
    # Сразу отдаём страницу: долгий сбор Telegram/бирж не должен блокировать ленту и бота.
    asyncio.create_task(_collect_manual())
    return RedirectResponse("/", status_code=303)


@app.post("/leads/{lead_id}/delete")
async def delete_lead(lead_id: int, next: str = Form("/taken")):
    conn = await db.connect()
    try:
        await db.delete_leads(conn, [lead_id], remember=True)
    finally:
        await conn.close()
    return RedirectResponse(next or "/taken", status_code=303)


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
    }
