from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import db
from app.filters import load_profile

log = logging.getLogger("leads.reply")

_jobs: dict[int, asyncio.Task] = {}
_guard = asyncio.Lock()


def _agy_bin() -> Path | None:
    from app.settings import settings

    for candidate in (
        (settings.agy_bin or "").strip(),
        (os.environ.get("AGY_BIN") or "").strip(),
    ):
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    if local.is_file():
        return local
    found = shutil.which("agy")
    return Path(found) if found else None


def _algorithm() -> str:
    path = Path(__file__).resolve().parents[3] / ".cursor" / "agents" / "otklik.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


def _offers() -> str:
    lines = []
    for service in load_profile().get("services") or []:
        title = (service.get("title") or "").strip()
        offer = (service.get("offer") or "").strip()
        if title and offer:
            lines.append(f"- {title}: {offer}")
    return "\n".join(lines)


def _prompt(lead: dict, about: str, about_kind: str, previous: str, instruction: str) -> str:
    who = (load_profile().get("who") or "").strip()
    task = (lead.get("text") or "").strip()[:3500]
    if about_kind == "user" and about:
        profile_note = f"Шапка профиля человека, можно опереться одной фразой:\n{about[:400]}"
    elif about_kind == "channel":
        profile_note = "Автор — канал или доска. Шапку канала в письмо не вставляй и не здоровайся её названием."
    else:
        profile_note = "Шапки профиля нет."
    parts = [
        _algorithm(),
        "",
        f"Кто пишет: Павел Клюшин. {who}",
        "Формулировки услуг, бери ту, что совпадает с задачей:",
        _offers(),
        "",
        profile_note,
        f"Имя в карточке: {lead.get('sender_name') or ''}",
        f"Канал: {lead.get('channel_title') or ''}",
        "",
        f"Текст заявки:\n{task}",
    ]
    if instruction and previous:
        parts.extend(
            [
                "",
                "Уже написанное письмо:",
                previous,
                "",
                "Замечание Павла. Перепиши письмо с учётом замечания. Остальные правила те же.",
                instruction.strip(),
            ]
        )
    parts.append("")
    parts.append("Верни только текст письма.")
    return "\n".join(parts)


def _agent_failure(text: str) -> str | None:
    low = (text or "").lower()
    if "authentication required" in low or "not authenticated" in low:
        return "Antigravity не залогинен — один раз запусти agy в терминале и войди в Google"
    if "out of usage" in low or "increase limits" in low or "usage limit" in low or "rate limit" in low:
        return "лимит Antigravity закончился — отклик сейчас не пишется"
    if "unauthorized" in low or "not logged in" in low or "sign in" in low:
        return "Antigravity не залогинен — один раз запусти agy в терминале и войди в Google"
    if "actionrequirederror" in low:
        return "Antigravity просит подтверждение входа"
    return None


def _plain_letter(text: str) -> str:
    cleaned = (text or "").strip()
    if _agent_failure(cleaned):
        return ""
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'«":
        cleaned = cleaned[1:-1].strip()
    hello = cleaned.find("Здравствуйте")
    if 0 < hello < 180:
        cleaned = cleaned[hello:]
    # Refuse model chatter that is not a letter.
    if cleaned and "Здравствуйте" not in cleaned and "Павел Клюшин" not in cleaned:
        if len(cleaned) < 40 or cleaned.lower().startswith(("error", "actionrequired", "i ")):
            return ""
    return cleaned.strip()


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        check=False,
    )


async def _ask_agy(prompt: str) -> tuple[str, str]:
    agy = _agy_bin()
    if not agy:
        return "", "Antigravity CLI не найден — поставь agy и перезапусти ящик"
    folder = Path(tempfile.mkdtemp(prefix="reply-"))
    try:
        (folder / "task.txt").write_text(prompt, encoding="utf-8")
        # Short argv: full letter brief lives in task.txt (Windows command-line limit).
        instruction = (
            "Open task.txt in this folder. Follow every instruction in it. "
            "Return only the finished letter text. No tools, no file edits, no preamble."
        )
        proc = await asyncio.create_subprocess_exec(
            str(agy),
            "-p",
            instruction,
            "--output-format",
            "text",
            "--disable-slash-commands",
            "--print-timeout",
            "3m",
            "--add-dir",
            str(folder),
            cwd=str(folder),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=200)
        except asyncio.TimeoutError:
            _kill_tree(proc.pid)
            log.warning("Antigravity не успел дописать отклик")
            return "", "Antigravity не успел за 3 минуты"
        out = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        combined = f"{out}\n{err_text}".strip()
        fail = _agent_failure(combined)
        if fail:
            log.warning("Antigravity отказал: %s", fail)
            return "", fail
        if proc.returncode not in (0, None):
            log.warning("Antigravity завершился с кодом %s: %s", proc.returncode, err_text[:400])
            return "", "Antigravity завершился с ошибкой"
        letter = _plain_letter(out)
        if not letter:
            hint = _agent_failure(err_text) or "Antigravity не ответил"
            return "", hint
        return letter, ""
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def _write(lead_id: int, instruction: str) -> None:
    conn = await db.connect()
    try:
        lead = await db.get_lead(conn, lead_id)
    finally:
        await conn.close()
    if not lead:
        return
    err = "Antigravity не ответил"
    text = ""
    try:
        from app.collector import author_profile

        profile = await author_profile(lead)
        previous = (lead.get("reply_text") or "").strip()
        prompt = _prompt(lead, profile["about"], profile["kind"], previous, instruction)
        text, fail = await _ask_agy(prompt)
        if fail:
            err = fail
    except Exception:
        log.exception("Отклик не собрался")
        err = "Antigravity не запустился"
        text = ""
    conn = await db.connect()
    try:
        if text:
            await db.save_reply(conn, lead_id, text)
            log.info("Отклик готов, заявка %s", lead_id)
        else:
            await db.mark_reply(conn, lead_id, "error", err)
            log.info("Отклик не вышел, заявка %s", lead_id)
    finally:
        await conn.close()


async def start_reply(lead_id: int) -> None:
    """Пишет отклик в фоне, если готового текста ещё нет."""
    async with _guard:
        job = _jobs.get(lead_id)
        if job and not job.done():
            return
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
            if not lead:
                return
            state = lead.get("reply_state") or ""
            if state == "writing":
                return
            if state == "ready" and (lead.get("reply_text") or "").strip():
                return
            await db.mark_reply(conn, lead_id, "writing")
        finally:
            await conn.close()
        _jobs[lead_id] = asyncio.create_task(_write(lead_id, ""))


async def finish_reply(lead_id: int, instruction: str) -> None:
    """Дожидается субагента. Пустое замечание — написать письмо заново."""
    async with _guard:
        previous = _jobs.get(lead_id)
    if previous and not previous.done():
        try:
            await previous
        except Exception:
            log.exception("Прошлый отклик оборвался")
    async with _guard:
        conn = await db.connect()
        try:
            lead = await db.get_lead(conn, lead_id)
            if not lead:
                return
            await db.mark_reply(conn, lead_id, "writing")
        finally:
            await conn.close()
        task = asyncio.create_task(_write(lead_id, instruction.strip()))
        _jobs[lead_id] = task
    await task
