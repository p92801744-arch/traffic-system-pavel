"""Рейтинг чатов для визитки. Ничего не отправляет.

Читает чаты из config/sources.yaml сессией Telethon на этом компьютере.
Второй клиент не открывает: просит слушателя Telegram (супервизор,
session_gate) отдать файл сессии на время прогона и вернуть его.
Если идёт вступление в чаты (data/JOIN_IN_PROGRESS), не стартует.
FloodWait ждёт. В чаты не вступает и не пишет.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import chat_rating, chat_scan, db  # noqa: E402
from app.collector import join_lock_active  # noqa: E402
from app.settings import settings  # noqa: E402


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


def _print_summary(summary: dict) -> None:
    print(
        f"Готово. Записано {summary.get('saved', 0)}, "
        f"пропущено свежих {summary.get('skipped', 0)}, "
        f"ошибок {len(summary.get('errors') or [])}."
    )
    for item in summary.get("errors") or []:
        print(f"  {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оценить чаты для визитки, ничего не отправляя")
    parser.add_argument("--only", default="", help="Один @чат")
    parser.add_argument("--limit", type=int, default=0, help="Сколько чатов с начала списка")
    parser.add_argument("--force", action="store_true", help="Читать заново, даже если смотрели недавно")
    parser.add_argument("--max-messages", type=int, default=500)
    parser.add_argument("--pause", type=float, default=1.5, help="Пауза между чатами, секунды")
    args = parser.parse_args(argv)

    if join_lock_active():
        print(
            "Идёт вступление в чаты (data\\JOIN_IN_PROGRESS). "
            "Сессию Telegram не открываю. Дождись конца join_chats.py и запусти скрипт ещё раз."
        )
        return 3

    if not settings.has_telegram_api:
        print(r"Нет TELEGRAM_API_ID / TELEGRAM_API_HASH в apps\leads\.env")
        return 2
    if not settings.session_file_exists:
        print(f"Нет файла сессии: {settings.session_path}.session")
        print("Сначала войди в ящик лидов на этом компьютере и подтверди код Telegram.")
        return 2

    print(
        "Прошу слушателя Telegram отдать сессию через session_gate. "
        "Второй клиент не открываю, в чаты не пишу."
    )
    try:
        summary = asyncio.run(
            chat_scan.run_guarded(
                only=args.only,
                limit=args.limit,
                force=args.force,
                max_messages=args.max_messages,
                pause=args.pause,
            )
        )
    except TimeoutError as exc:
        print(f"Сессию не взял: {exc}")
        print(
            "Слушатель не отдал сессию или она занята. "
            "Второй клиент не открываю. Подожди минуту и запусти скрипт ещё раз."
        )
        return 3
    except RuntimeError as exc:
        text = str(exc)
        if "вступление" in text:
            print(text)
            return 3
        print(f"Прогон остановился: {exc}")
        return 4
    except Exception as exc:
        print(f"Прогон остановился: {exc}")
        return 4

    _print_summary(summary)
    rows = asyncio.run(_load_rows())
    _print_rows(rows)
    _print_how_to_read()
    return 4 if summary.get("stopped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
