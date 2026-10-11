"""Замок одного экземпляра, пауза при обрыве, журналы и пульс процессов."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from app.settings import ROOT, settings

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
STABLE_SECONDS = 30.0
LOG_MAX_BYTES = 1_048_576
LOG_BACKUPS = 5


class AlreadyRunning(RuntimeError):
    """Другой процесс уже держит этот замок."""


def current_role() -> str:
    return os.environ.get("LEADS_ROLE", "all").strip().lower() or "all"


def embed_workers() -> bool:
    """Ручной `python -m app` сам поднимает сборщики. Автозапуск — нет."""
    return current_role() in ("all", "embedded")


def runtime_dir() -> Path:
    override = os.environ.get("LEADS_RUNTIME_DIR", "").strip()
    path = Path(override) if override else ROOT / "data" / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    override = os.environ.get("LEADS_LOG_DIR", "").strip()
    path = Path(override) if override else ROOT / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 259 — процесс ещё работает. 87 — такого pid нет. 5 — есть, но нет прав смотреть.
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def interpret_windows_liveness(opened: bool, error: int, exit_code: int | None) -> bool:
    """Результат OpenProcess + GetExitCodeProcess. Без os.kill: на Windows сигнал 0 ломает Python 3.11."""
    if not opened:
        return error == _ERROR_ACCESS_DENIED
    if exit_code is None:
        return error == _ERROR_ACCESS_DENIED
    return exit_code == _STILL_ACTIVE


def _query_windows_process(pid: int) -> tuple[bool, int, int | None]:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False, ctypes.get_last_error(), None
    code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True, ctypes.get_last_error(), None
        return True, 0, int(code.value)
    finally:
        kernel32.CloseHandle(handle)


def _windows_pid_alive(pid: int) -> bool:
    opened, error, exit_code = _query_windows_process(pid)
    return interpret_windows_liveness(opened, error, exit_code)


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        # SystemError здесь — сломанный os.kill, который на Windows ещё и роняет проверку.
        return False
    return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            return _windows_pid_alive(pid)
        return _posix_pid_alive(pid)
    except Exception:
        return False


def backoff_seconds(attempt: int, *, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS) -> float:
    step = max(1, int(attempt))
    return min(cap, base * (2 ** (step - 1)))


def _open_lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            fd = None
        else:
            os.write(fd, b"\0")
            return os.fdopen(fd, "r+b", buffering=0)
    stream = open(path, "r+b", buffering=0)
    stream.seek(0, os.SEEK_END)
    if stream.tell() < 1:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    return stream


def _lock_nb(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SingleInstance:
    """Эксклюзивный замок на файл. Второй процесс получает AlreadyRunning.

    На Unix flock держится процессом, не потоком: проверка «уже занято»
    из того же процесса замок не увидит. Для этого и нужен отдельный процесс.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = _open_lock_file(self.path)
        try:
            _lock_nb(self._stream)
        except OSError as exc:
            self._stream.close()
            self._stream = None
            raise AlreadyRunning(str(self.path)) from exc

    def write_pid(self, pid: int | None = None) -> None:
        if self._stream is None:
            return
        text = f"{pid or os.getpid()}\n".encode("ascii")
        self._stream.seek(0)
        self._stream.write(text)
        self._stream.truncate(len(text))
        self._stream.flush()
        try:
            os.fsync(self._stream.fileno())
        except OSError:
            pass

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        _unlock(stream)
        stream.close()

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def lock_held(path: Path) -> bool:
    """True, если замок сейчас занят другим процессом."""
    if not Path(path).exists():
        return False
    try:
        held = SingleInstance(path)
    except AlreadyRunning:
        return True
    held.close()
    return False


class RotatingLog:
    """Дописывает строки в файл и режет его по размеру. Пишет только этот процесс."""

    def __init__(self, path: Path, *, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path, "a", encoding="utf-8", newline="\n")
        self._size = self.path.stat().st_size if self.path.exists() else 0

    def write(self, text: str) -> None:
        if text is None:
            return
        line = text if text.endswith("\n") else text + "\n"
        data = line.encode("utf-8", errors="replace")
        if self._size and self._size + len(data) > self.max_bytes:
            self._rotate()
        self._stream.write(line)
        self._stream.flush()
        self._size += len(data)

    def close(self) -> None:
        self._stream.close()

    def _rotate(self) -> None:
        self._stream.close()
        for index in range(self.backups, 1, -1):
            src = self.path.with_name(f"{self.path.name}.{index - 1}")
            dst = self.path.with_name(f"{self.path.name}.{index}")
            if not src.exists():
                continue
            if dst.exists():
                dst.unlink()
            src.replace(dst)
        archived = self.path.with_name(f"{self.path.name}.1")
        if self.path.exists():
            if archived.exists():
                archived.unlink()
            self.path.replace(archived)
        self._stream = open(self.path, "a", encoding="utf-8", newline="\n")
        self._size = 0


def network_up(probe=None) -> bool:
    check = probe or _default_probe
    try:
        return bool(check())
    except Exception:
        return False


def _default_probe() -> bool:
    # IP, без DNS: сразу после включения имя ещё может не резолвиться.
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            continue
    return False


async def wait_for_network(probe=None, sleep=None, *, base: float = 2, cap: float = 30, on_wait=None) -> None:
    import asyncio

    pause = sleep or asyncio.sleep
    delay = base
    while not network_up(probe):
        if on_wait is not None:
            on_wait(delay)
        await pause(delay)
        delay = min(cap, max(base, delay * 2))


def heartbeat_path(role: str) -> Path:
    return runtime_dir() / f"{role}.json"


def write_heartbeat(role: str, **fields) -> None:
    payload = {
        "role": role,
        "pid": os.getpid(),
        "heartbeat_at": iso_now(),
    }
    payload.update(fields)
    path = heartbeat_path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_heartbeat(role: str) -> dict | None:
    path = heartbeat_path(role)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_fresh(data: dict, max_age: float) -> bool:
    raw = str(data.get("heartbeat_at") or "")
    if not raw:
        return False
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    return 0 <= age <= max_age


def local_site() -> str:
    host = settings.host or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.port}"


def peek_telegram_authorized() -> bool:
    """Не открывает сессию Telethon: файл сессии появляется только после входа."""
    if settings.session_file_exists:
        return True
    data = read_heartbeat("telegram")
    if data and heartbeat_fresh(data, 90):
        return bool(data.get("authorized"))
    return False


def write_owner(kind: str, pid: int | None = None) -> None:
    payload = {"kind": kind, "pid": pid or os.getpid(), "started_at": iso_now()}
    path = runtime_dir() / "owner.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def read_owner() -> dict | None:
    path = runtime_dir() / "owner.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_owner(pid: int | None = None) -> None:
    path = runtime_dir() / "owner.json"
    current = read_owner()
    if current and pid is not None and int(current.get("pid") or 0) != pid:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
