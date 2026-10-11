"""Рейтинг чатов для визитки. Ничего не отправляет.

Читает чаты из config/sources.yaml сессией Telethon на этом компьютере.
Если ящик лидов уже запущен, второй клиент не открывает: просит ящик
прочитать чаты самому. FloodWait ждёт. В чаты не вступает и не пишет.

Запуск на Asus, из PowerShell, ящик уже поднят автозапуском
scripts\\install-autostart.ps1:

    cd apps\\leads
    .\\.venv\\Scripts\\python.exe scripts\\rate_chats.py

Полный прогон заново, даже если чат смотрели меньше 12 часов назад:

    .\\.venv\\Scripts\\python.exe scripts\\rate_chats.py --force

Один чат:

    .\\.venv\\Scripts\\python.exe scripts\\rate_chats.py --only freelansim_ru

Как читать результат:
открой http://127.0.0.1:8787/chats
Рейтинг 0–100, выше — ближе к визитке.
«Можно визитку» стоит в плане, дата в колонке «Следующая».
«Реклама запрещена», «Спам» и «Неясно» в план не попадают.
«Почему» — причина статуса. Ручной статус в выпадающем списке
не сбрасывается следующим прогоном.
Черновик внутри строки. Его никто не отправляет.
База: apps\\leads\\data\\leads.db, таблицы chat_profiles и chat_rules.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import chat_rating, chat_scan, db  # noqa: E402
from app.session_gate import SessionBusy, ensure, plan_run, release, session_sqlite_busy  # noqa: E402
from app.settings import settings  # noqa: E402


def probe_inbox() -> dict | None:
    base = f"http://127.0.0.1:{settings.port}"
    try:
        response = httpx.get(f"{base}/health", timeout=2.0)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def resolve_mode(health: dict | None) -> str:
    inbox_up = bool(health and health.get("ok"))
    can_delegate = bool(health and health.get("chat_rate"))
    if inbox_up:
        return plan_run(inbox_up=True, can_delegate=can_delegate, session_busy=False)
    return plan_run(inbox_up=False, can_delegate=False, session_busy=session_sqlite_busy())


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("В базе ещё нет чатов.")
        return
    print(f"{'Рейтинг':>7}  {'Статус':<20}  Чат")
    for row in rows:
        status = chat_rating.effective_status(row)
        label = chat_rating.STATUS_LABELS.get(status, status)
        name = row.get("username") or row.get("chat_key")
        when = (row.get("next_card_at") or "—").replace("T", " ")[:16]
        print(f"{row.get('rating') or 0:7}  {label:<20}  @{name}  {when}")
        reason = chat_rating.display_reason(row)
        if reason:
            print(f"         {reason}")


def _print_how_to_read() -> None:
    print()
    print("Как читать: открой http://127.0.0.1:8787/chats")
    print("«Можно визитку» — в плане, дата в колонке «Следующая». Остальные статусы в план не ставятся.")
    print("Черновик в строке чата. Отправки нет: копируешь и отправляешь сам.")
    print(r"База: apps\leads\data\leads.db, таблицы chat_profiles и chat_rules.")


async def _load_rows() -> list[dict]:
    conn = await db.connect()
    try:
        return await db.list_chat_profiles(conn, "rating", "desc")
    finally:
        await conn.close()


def _busy_message(health: dict | None) -> str:
    if health and health.get("ok") and not health.get("chat_rate"):
        return (
            "Ящик на 127.0.0.1:8787 уже держит сессию Telegram, но это ещё старый процесс "
            "без прогона рейтинга. Второй клиент не открываю.\n"
            "Перезапусти ящик один раз, чтобы он подхватил этот код "
            "(останови python -m app и дай scripts\\install-autostart.ps1 поднять его снова), "
            "затем запусти скрипт ещё раз. Он попросит работающий ящик прочитать чаты сам."
        )
    if health and health.get("ok"):
        return (
            "Ящик лидов уже запущен. Второй клиент не открываю: "
            "прогон должен идти внутри ящика. Если видишь это сообщение, "
            "ящик не принял запрос. Подожди минуту и запусти скрипт ещё раз."
        )
    return (
        "Сессия Telegram занята другим процессом (ящик, вступление в чаты или прошлый прогон). "
        "Второй клиент не открываю. Дождись, пока сессия освободится, и запусти скрипт ещё раз."
    )


def delegate(only: str, limit: int, force: bool, max_messages: int) -> int:
    base = f"http://127.0.0.1:{settings.port}"
    params = {"only": only, "limit": limit, "force": 1 if force else 0, "max_messages": max_messages}
    print("Ящик уже держит сессию. Прошу его прочитать чаты, второй клиент не открываю.")
    try:
        started = httpx.post(f"{base}/internal/chat-rate", params=params, timeout=10.0)
        started.raise_for_status()
    except Exception as exc:
        print(f"Ящик не принял прогон: {exc}")
        print(_busy_message({"ok": True, "chat_rate": True}))
        return 3
    deadline = time.monotonic() + 60 * 60 * 6
    last = ""
    while time.monotonic() < deadline:
        try:
            payload = httpx.get(f"{base}/internal/chat-rate", timeout=10.0).json()
        except Exception as exc:
            print(f"Ящик перестал отвечать: {exc}")
            return 3
        current = f"{payload.get('done')}/{payload.get('total')} {payload.get('current') or ''}".strip()
        if current and current != last:
            print(f"  {current}")
            last = current
        if payload.get("finished_at") and not payload.get("running"):
            summary = payload.get("summary") or {}
            if payload.get("error"):
                print(payload["error"])
            print(
                f"Готово. Записано {summary.get('saved', 0)}, "
                f"пропущено свежих {summary.get('skipped', 0)}, "
                f"ошибок {len(summary.get('errors') or [])}."
            )
            for item in summary.get("errors") or []:
                print(f"  {item}")
            rows = asyncio.run(_load_rows())
            _print_rows(rows)
            _print_how_to_read()
            return 4 if summary.get("stopped") else 0
        time.sleep(3)
    print("Прогон не закончился за отведённое время. Страница «Чаты» покажет то, что уже записано.")
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оценить чаты для визитки, ничего не отправляя")
    parser.add_argument("--only", default="", help="Один @чат")
    parser.add_argument("--limit", type=int, default=0, help="Сколько чатов с начала списка")
    parser.add_argument("--force", action="store_true", help="Читать заново, даже если смотрели недавно")
    parser.add_argument("--max-messages", type=int, default=500)
    parser.add_argument("--pause", type=float, default=1.5, help="Пауза между чатами, секунды")
    args = parser.parse_args(argv)

    health = probe_inbox()
    mode = resolve_mode(health)
    if mode == "delegate":
        return delegate(args.only, args.limit, args.force, args.max_messages)
    if mode == "busy":
        print(_busy_message(health))
        return 3

    if not settings.has_telegram_api:
        print(r"Нет TELEGRAM_API_ID / TELEGRAM_API_HASH в apps\leads\.env")
        return 2
    if not settings.session_file_exists:
        print(f"Нет файла сессии: {settings.session_path}.session")
        print("Сначала войди в ящик лидов на этом компьютере и подтверди код Telegram.")
        return 2
    try:
        ensure("rate")
    except SessionBusy as exc:
        print(f"Сессию не беру: {exc}")
        print(_busy_message(None))
        return 3

    print("Ящик на этом порту не отвечает. Читаю чаты этой сессией и никуда не пишу.")

    async def _local():
        try:
            return await chat_scan.run_scan(
                only=args.only,
                limit=args.limit,
                force=args.force,
                max_messages=args.max_messages,
                pause=args.pause,
            )
        finally:
            from app import collector

            await collector.disconnect()

    try:
        try:
            summary = asyncio.run(_local())
        except Exception as exc:
            print(f"Прогон остановился: {exc}")
            return 4
    finally:
        release()
    print(
        f"Готово. Записано {summary.get('saved', 0)}, "
        f"пропущено свежих {summary.get('skipped', 0)}, "
        f"ошибок {len(summary.get('errors') or [])}."
    )
    for item in summary.get("errors") or []:
        print(f"  {item}")
    rows = asyncio.run(_load_rows())
    _print_rows(rows)
    _print_how_to_read()
    return 4 if summary.get("stopped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
