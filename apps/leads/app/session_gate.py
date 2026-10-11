"""Один файл сессии Telethon — один процесс.

Слушатель держит сессию, пока идёт живая лента. Сайт, когда нужно войти
или прочитать профиль, просит паузу, дожидается отпускания и берёт замок сам.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app.servicekit import AlreadyRunning, SingleInstance, pid_alive

BORROW_NAME = "session.borrow"
YIELDED_NAME = "session.yielded"
OWNER_NAME = "session.owner"
LOCK_NAME = "session.lock"


class SessionOwner:
    def __init__(self, runtime: Path):
        self.runtime = Path(runtime)
        self._lock: SingleInstance | None = None

    def acquire(self) -> None:
        """Ждёт замок. Пока сайт держит сессию, слушатель стоит здесь."""
        self.runtime.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._lock = SingleInstance(self.runtime / LOCK_NAME)
                break
            except AlreadyRunning:
                time.sleep(0.2)
        self._lock.write_pid()
        (self.runtime / OWNER_NAME).write_text(str(os.getpid()), encoding="ascii")
        if not self.borrow_requested():
            self.clear_yielded()

    def release(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        owner = self.runtime / OWNER_NAME
        try:
            if owner.exists() and owner.read_text(encoding="ascii").strip() == str(os.getpid()):
                owner.unlink()
        except OSError:
            pass

    def borrow_requested(self) -> bool:
        return (self.runtime / BORROW_NAME).is_file()

    def mark_yielded(self) -> None:
        (self.runtime / YIELDED_NAME).write_text(str(os.getpid()), encoding="ascii")

    def clear_yielded(self) -> None:
        path = self.runtime / YIELDED_NAME
        if path.exists():
            path.unlink()


def owner_alive(runtime: Path) -> bool:
    path = Path(runtime) / OWNER_NAME
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    return pid_alive(pid)


def _borrow_stale(runtime: Path, max_age: float) -> bool:
    path = Path(runtime) / BORROW_NAME
    if not path.exists():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > max_age


async def wait_borrow_finished(owner: SessionOwner, *, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    request = owner.runtime / BORROW_NAME
    while request.exists():
        if time.monotonic() >= deadline or _borrow_stale(owner.runtime, timeout):
            try:
                request.unlink()
            except OSError:
                pass
            break
        await asyncio.sleep(0.2)
    owner.clear_yielded()


@asynccontextmanager
async def borrow_session(runtime: Path, *, timeout: float = 40):
    """Забирает сессию у слушателя на время блока и возвращает её обратно."""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    request = runtime / BORROW_NAME
    asked = False
    if owner_alive(runtime):
        request.write_text(str(os.getpid()), encoding="ascii")
        asked = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (runtime / YIELDED_NAME).exists() or not owner_alive(runtime):
                break
            await asyncio.sleep(0.2)
        else:
            request.unlink(missing_ok=True)
            raise TimeoutError("Слушатель Telegram не отдал сессию")
    lock: SingleInstance | None = None
    try:
        lock = await asyncio.to_thread(_acquire_lock, runtime / LOCK_NAME, timeout)
        yield
    finally:
        if lock is not None:
            lock.close()
        if asked:
            request.unlink(missing_ok=True)


def _acquire_lock(path: Path, timeout: float) -> SingleInstance:
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        try:
            return SingleInstance(path)
        except AlreadyRunning as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("Сессия Telegram занята") from exc
            time.sleep(0.1)


def _hold_cli(runtime: Path) -> None:
    """Тестовый слушатель: держит замок, пока родитель не попросит паузу."""
    owner = SessionOwner(runtime)
    owner.acquire()
    print("ready", flush=True)
    deadline = time.time() + 10
    while not owner.borrow_requested():
        if time.time() > deadline:
            owner.release()
            raise SystemExit(1)
        time.sleep(0.05)
    owner.mark_yielded()
    owner.release()
    print("yielded", flush=True)
    deadline = time.time() + 10
    while (runtime / BORROW_NAME).exists() and time.time() < deadline:
        time.sleep(0.05)
    print("done", flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "hold":
        _hold_cli(Path(sys.argv[2]))
