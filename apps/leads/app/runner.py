"""Долгие процессы ящика: Telegram, биржи, встречи, бот.

Каждый запускается отдельно, чтобы падение одного не гасило остальные.
Сеть после включения компьютера может появиться не сразу — ждём и повторяем.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from app import boards, collector, networking, notifier
from app.servicekit import (
    AlreadyRunning,
    SingleInstance,
    runtime_dir,
    wait_for_network,
    write_heartbeat,
)
from app.session_gate import SessionOwner, wait_borrow_finished
from app.settings import settings

log = logging.getLogger("leads.runner")

ROLES = ("telegram", "boards", "networking", "notifier")
_history_done = False


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def _network_detail(delay: float) -> str:
    return f"Жду сеть, повтор через {int(delay)} с."


async def _sleep_working(role: str, seconds: float, detail: str) -> None:
    end = time.monotonic() + seconds
    while True:
        write_heartbeat(role, state="running", detail=detail)
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(5, left))


async def _serve_poll(role: str, once, pause: float, detail: str) -> None:
    delay = 5.0
    while True:
        await wait_for_network(
            on_wait=lambda seconds: write_heartbeat(role, state="waiting_network", detail=_network_detail(seconds))
        )
        try:
            await once()
        except Exception:
            log.exception("Сбор %s оборвался", role)
            write_heartbeat(role, state="retry", detail="Ошибка сбора, повторю.")
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 2)
            continue
        delay = 5.0
        await _sleep_working(role, pause, detail)


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _history_safe() -> None:
    global _history_done
    try:
        result = await collector.collect_history()
        _history_done = True
        log.info("Сбор истории: %s", result)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Не удалось собрать историю")


async def _watch_pause(owner: SessionOwner, client) -> None:
    runtime = runtime_dir()
    last = 0.0
    while True:
        now = time.monotonic()
        if now - last >= 5:
            write_heartbeat("telegram", state="running", authorized=True, detail="Живая лента включена.")
            last = now
        if owner.borrow_requested():
            try:
                await client.disconnect()
            except Exception:
                log.exception("Не удалось отпустить сессию Telegram")
            return
        try:
            await collector.answer_profile_request(runtime)
        except Exception:
            log.exception("Не удалось прочитать шапку профиля")
        await asyncio.sleep(0.3)


async def _connected_session(owner: SessionOwner) -> str:
    client = collector.make_client()
    history: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            write_heartbeat(
                "telegram",
                state="need_login",
                authorized=False,
                detail="Открой сайт и войди в Telegram.",
            )
            return "unauthorized"
        write_heartbeat("telegram", state="running", authorized=True, detail="Живая лента включена.")
        watcher = asyncio.create_task(_watch_pause(owner, client))
        await collector.start_listener()
        if not _history_done:
            history = asyncio.create_task(_history_safe())
        await client.run_until_disconnected()
        if owner.borrow_requested():
            return "paused"
        return "dropped"
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Telegram оборвался")
        if owner.borrow_requested():
            return "paused"
        return "dropped"
    finally:
        await _cancel(watcher)
        await _cancel(history)
        await collector.disconnect()


async def _telegram_turn(owner: SessionOwner) -> str:
    outcome = "dropped"
    await asyncio.to_thread(owner.acquire)
    try:
        if owner.borrow_requested():
            outcome = "paused"
        else:
            outcome = await _connected_session(owner)
    except asyncio.CancelledError:
        outcome = "paused" if owner.borrow_requested() else "dropped"
        raise
    except Exception:
        log.exception("Telegram оборвался")
        outcome = "paused" if owner.borrow_requested() else "dropped"
    finally:
        if outcome == "paused" or owner.borrow_requested():
            owner.mark_yielded()
            outcome = "paused"
        owner.release()
    return outcome


async def serve_telegram() -> None:
    owner = SessionOwner(runtime_dir())
    delay = 5.0
    while True:
        await wait_for_network(
            on_wait=lambda seconds: write_heartbeat(
                "telegram",
                state="waiting_network",
                authorized=settings.session_file_exists,
                detail=_network_detail(seconds),
            )
        )
        outcome = await _telegram_turn(owner)
        if outcome == "paused":
            await wait_borrow_finished(owner)
            delay = 5.0
            continue
        if outcome == "unauthorized":
            for _ in range(10):
                if settings.session_file_exists:
                    break
                write_heartbeat(
                    "telegram",
                    state="need_login",
                    authorized=False,
                    detail="Открой сайт и войди в Telegram.",
                )
                await asyncio.sleep(1)
            delay = 5.0
            continue
        write_heartbeat(
            "telegram",
            state="retry",
            authorized=settings.session_file_exists,
            detail="Связь оборвалась, повторю.",
        )
        await asyncio.sleep(delay)
        delay = min(120.0, delay * 2)


async def serve_boards() -> None:
    await _serve_poll("boards", boards.poll_once, boards.PAUSE_SECONDS, "Собираю заказы с бирж.")


async def serve_networking() -> None:
    await _serve_poll("networking", networking.refresh, networking.PAUSE_SECONDS, "Собираю встречи.")


async def serve_notifier() -> None:
    delay = 5.0
    while True:
        if not notifier.enabled():
            write_heartbeat("notifier", state="idle", detail="BOT_TOKEN пустой, уведомления выключены.")
            await asyncio.sleep(30)
            continue
        await wait_for_network(
            on_wait=lambda seconds: write_heartbeat("notifier", state="waiting_network", detail=_network_detail(seconds))
        )
        try:
            started = await notifier.start()
        except Exception:
            log.exception("Бот не стартовал")
            started = False
        if not started:
            write_heartbeat("notifier", state="retry", detail="Не удалось подключиться к Bot API, повторю.")
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 2)
            continue
        delay = 5.0
        while notifier.running():
            write_heartbeat("notifier", state="running", detail="Бот слушает сообщения.")
            await asyncio.sleep(5)
        await notifier.stop()
        write_heartbeat("notifier", state="retry", detail="Цикл бота остановился, запускаю снова.")
        await asyncio.sleep(delay)
        delay = min(120.0, delay * 2)


async def _amain(role: str) -> None:
    if role == "telegram":
        await serve_telegram()
    elif role == "boards":
        await serve_boards()
    elif role == "networking":
        await serve_networking()
    elif role == "notifier":
        await serve_notifier()
    else:
        raise RuntimeError(f"неизвестная роль: {role}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ROLES:
        print("Нужна роль: telegram, boards, networking, notifier", file=sys.stderr)
        return 2
    role = args[0]
    os.environ["LEADS_ROLE"] = role
    try:
        lock = SingleInstance(runtime_dir() / f"{role}.lock")
    except AlreadyRunning:
        print(f"{role} уже запущен", file=sys.stderr)
        return 0
    lock.write_pid()
    try:
        _configure_logging()
        asyncio.run(_amain(role))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
