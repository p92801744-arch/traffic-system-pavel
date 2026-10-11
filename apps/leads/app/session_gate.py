"""Один файл сессии Telethon — один процесс.

Ящик лидов, вступление в чаты и прогон рейтинга делят `data/telegram.session`.
Второй клиент на том же файле ломает SQLite и может разлогинить аккаунт.
Кто первый взял блокировку, тот и держит сессию. Остальные ждут или
просят уже запущенный ящик сделать работу сам.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.settings import settings

_state: dict = {"fh": None, "role": ""}


class SessionBusy(RuntimeError):
    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        role = self.owner.get("role") or "другой процесс"
        pid = self.owner.get("pid") or "?"
        super().__init__(f"сессию Telegram держит {role}, pid {pid}")


def lock_path() -> Path:
    base = Path(settings.session_path)
    return base.with_name(base.name + ".session.lock")


def owner_path() -> Path:
    base = Path(settings.session_path)
    return base.with_name(base.name + ".session.owner")


def read_owner() -> dict:
    path = owner_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def held_by_us() -> bool:
    return _state["fh"] is not None


def ensure(role: str) -> None:
    """Берёт блокировку, если её ещё нет у этого процесса. Иначе SessionBusy."""
    if _state["fh"] is not None:
        return
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115 — держим дескриптор, пока живёт процесс
    try:
        _lock(fh)
    except OSError:
        fh.close()
        raise SessionBusy(read_owner()) from None
    _state["fh"] = fh
    _state["role"] = role
    payload = {"pid": os.getpid(), "role": role}
    try:
        owner_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def release() -> None:
    fh = _state["fh"]
    if fh is None:
        return
    try:
        _unlock(fh)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
    _state["fh"] = None
    _state["role"] = ""
    try:
        owner_path().unlink(missing_ok=True)
    except OSError:
        pass


def session_sqlite_busy() -> bool:
    """True, если файл сессии уже открыт на запись. Сам сессию не забирает надолго."""
    path = Path(str(settings.session_path) + ".session")
    if not path.exists():
        return False
    import sqlite3

    conn = sqlite3.connect(str(path), timeout=0.2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return False
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc).lower() or "busy" in str(exc).lower()
    finally:
        conn.close()


def plan_run(*, inbox_up: bool, can_delegate: bool, session_busy: bool) -> str:
    """delegate — прогон внутри ящика; busy — сессию не трогаем; local — сами."""
    if inbox_up and can_delegate:
        return "delegate"
    if inbox_up or session_busy:
        return "busy"
    return "local"


def _lock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
