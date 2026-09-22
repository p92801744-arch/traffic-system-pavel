from __future__ import annotations

import json
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import collector, db, lessons, notifier, scorer
from app.filters import configured_channels, intake_hits, load_sources
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
    await conn.close()
    asyncio.create_task(_rescore_safe())
    try:
        if await collector.is_authorized():
            await collector.start_listener()
            asyncio.create_task(_collect_history_safe())
    except Exception:
        log.exception("Telegram ещё не готов")
    await notifier.start()
    yield
    await notifier.stop()
    await collector.disconnect()


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
            status=status or None,
            channel=channel or None,
            hide_low=not bool(show_low),
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
        status=status or "",
        channel=channel or "",
        show_low=bool(show_low),
        scorer_on=True,
        lessons=lessons.load_lessons(8),
    )


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
        await collector.start_listener()
        try:
            await collector.collect_history()
        except Exception:
            log.exception("Сбор после входа не удался")
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
        await db.reject_lead(conn, lead_id, text)
    finally:
        await conn.close()
    lessons.add_lesson(text, lead_id)
    return RedirectResponse(next or "/", status_code=303)


@app.post("/collect")
async def collect_now():
    try:
        await collector.refresh_watch_list()
        await collector.collect_history()
    except Exception:
        log.exception("Ручной сбор не удался")
    return RedirectResponse("/", status_code=303)


@app.get("/health")
async def health():
    authorized = False
    try:
        authorized = await collector.is_authorized()
    except Exception:
        pass
    return {
        "ok": True,
        "telegram_api": settings.has_telegram_api,
        "authorized": authorized,
        "scorer": True,
        "bot": bool(settings.bot_token),
    }
