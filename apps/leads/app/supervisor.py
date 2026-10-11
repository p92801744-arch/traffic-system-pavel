"""Держит процессы ящика живыми и не даёт подняться второй копии.

Запуск: python -m app.supervisor run
Проверка: python -m app.supervisor status
Остановка: python -m app.supervisor stop
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from app.servicekit import (
    LOG_BACKUPS,
    LOG_MAX_BYTES,
    AlreadyRunning,
    RotatingLog,
    SingleInstance,
    atomic_json,
    backoff_seconds,
    clear_owner,
    iso_now,
    local_site,
    lock_held,
    logs_dir,
    pid_alive,
    read_heartbeat,
    read_json,
    read_owner,
    runtime_dir,
    write_owner,
)
from app.settings import ROOT, settings

TASK_NAME = "PavelLeadsInbox"


@dataclass(frozen=True)
class ChildSpec:
    name: str
    title: str
    args: list[str]
    env: dict[str, str]


CHILD_SPECS: tuple[ChildSpec, ...] = (
    ChildSpec("web", "Сайт", ["-m", "app"], {"LEADS_ROLE": "web"}),
    ChildSpec("telegram", "Telegram", ["-m", "app.runner", "telegram"], {"LEADS_ROLE": "telegram"}),
    ChildSpec("boards", "Биржи", ["-m", "app.runner", "boards"], {"LEADS_ROLE": "boards"}),
    ChildSpec("networking", "Встречи", ["-m", "app.runner", "networking"], {"LEADS_ROLE": "networking"}),
    ChildSpec("notifier", "Бот", ["-m", "app.runner", "notifier"], {"LEADS_ROLE": "notifier"}),
)


@dataclass
class Child:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    pump: threading.Thread | None = None
    log: RotatingLog | None = None
    restarts: int = 0
    last_exit: int | None = None
    started_at: str | None = None
    started_mono: float = 0
    attempt: int = 0
    next_start: float = 0
    state: str = "starting"


def install_hint() -> str:
    script = ROOT / "scripts" / "install-autostart.ps1"
    return f'powershell -ExecutionPolicy Bypass -File "{script}"'


def probe_health(url: str, timeout: float = 3) -> tuple[bool, dict | None, str]:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return True, data if isinstance(data, dict) else None, ""
    except Exception as exc:
        return False, None, str(exc)


def format_status(
    *,
    owner_running: bool,
    owner: dict | None,
    snapshot: dict | None,
    heartbeats: dict[str, dict | None],
    health: tuple[bool, dict | None, str],
    logs: Path,
    alive=pid_alive,
) -> tuple[list[str], int]:
    site = local_site()
    lines: list[str] = []
    if not owner_running:
        lines.append("Автозапуск не запущен.")
        lines.append(f"Поставить его один раз: {install_hint()}")
        lines.append(f"Журналы: {logs}")
        return lines, 1

    kind = str((owner or {}).get("kind") or "")
    owner_pid = int((owner or {}).get("pid") or 0)
    if kind == "manual":
        lines.append("Ящик сейчас запущен вручную, командой python -m app.")
        lines.append("Автозапуск при этом не работает. Закрой ручное окно и войди в Windows ещё раз, либо поставь задачу заново.")
        _append_health(lines, site, health)
        lines.append(f"Журналы: {logs}")
        return lines, 1

    if kind != "supervisor" or not alive(owner_pid):
        lines.append("Замок автозапуска занят, но живой супервизор не найден.")
        lines.append("Останови его командой python -m app.supervisor stop и поставь задачу ещё раз.")
        lines.append(f"Журналы: {logs}")
        return lines, 1

    lines.append(f"Автозапуск работает, процесс {owner_pid}.")
    children = (snapshot or {}).get("children") or {}
    failed = False
    for spec in CHILD_SPECS:
        row = children.get(spec.name) or {}
        state = str(row.get("state") or "")
        pid = int(row.get("pid") or 0)
        detail = str((heartbeats.get(spec.name) or {}).get("detail") or "").strip()
        restarts = int(row.get("restarts") or 0)
        if state == "running" and alive(pid):
            sentence = f"{spec.title} работает, процесс {pid}."
            if detail:
                sentence += f" {detail if detail.endswith('.') else detail + '.'}"
            if restarts:
                sentence += f" Перезапускался {restarts} раз."
            lines.append(sentence)
            continue
        failed = True
        if state == "backoff":
            retry_at = row.get("retry_at")
            left = 0
            if isinstance(retry_at, (int, float)):
                left = max(0, int(retry_at - time.time()))
            code = row.get("last_exit")
            extra = f" Прошлый код выхода {code}." if code is not None else ""
            lines.append(f"{spec.title} сейчас перезапускается, следующая попытка через {left} с.{extra}")
        else:
            lines.append(f"{spec.title} не запущен.")
    if not _append_health(lines, site, health):
        failed = True
    lines.append(f"Журналы: {logs}")
    return lines, 1 if failed else 0


def _append_health(lines: list[str], site: str, health: tuple[bool, dict | None, str]) -> bool:
    ok, body, _error = health
    if not ok or not body or not body.get("ok"):
        lines.append(f"Страница не открывается: {site}")
        return False
    lines.append(f"Страница открывается: {site}")
    if body.get("authorized"):
        lines.append("Вход в Telegram есть.")
    elif body.get("telegram_api"):
        lines.append("Вход в Telegram ещё не сделан. Открой сайт и введи код.")
    if body.get("bot"):
        lines.append("Токен бота на месте.")
    else:
        lines.append("Токен бота пустой, уведомления в Telegram выключены.")
    return True


class WinJob:
    """На Windows дочерние процессы умирают вместе с супервизором, даже если его убили извне."""

    def __init__(self, handle):
        self.handle = handle

    @classmethod
    def try_create(cls) -> WinJob | None:
        if os.name != "nt":
            return None
        try:
            return cls(_create_kill_on_close_job())
        except Exception:
            return None

    def assign(self, pid: int) -> bool:
        try:
            _assign_pid_to_job(self.handle, pid)
            return True
        except Exception:
            return False

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if not handle:
            return
        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(handle)


def _create_kill_on_close_job():
    import ctypes
    from ctypes import wintypes

    job_object_extended_limit_information = 9
    kill_on_job_close = 0x2000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = kill_on_job_close
    ok = kernel32.SetInformationJobObject(
        handle,
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _assign_pid_to_job(handle, pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    process_set_quota = 0x0100
    process_terminate = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.AssignProcessToJobObject(handle, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(process)


class Supervisor:
    def __init__(
        self,
        specs: tuple[ChildSpec, ...] | list[ChildSpec] = CHILD_SPECS,
        *,
        root: Path | None = None,
        runtime: Path | None = None,
        logs: Path | None = None,
        executable: str | None = None,
        launch=None,
        clock=None,
        sleep=None,
        interval: float = 0.5,
        base_delay: float = 1,
        cap_delay: float = 60,
        stable_after: float = 30,
        log_max_bytes: int = LOG_MAX_BYTES,
        log_backups: int = LOG_BACKUPS,
    ):
        self.root = Path(root or ROOT)
        self.runtime = Path(runtime or runtime_dir())
        self.logs = Path(logs or logs_dir())
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.executable = executable or sys.executable
        self._launch = launch or self._popen
        self._clock = clock or time.monotonic
        self._sleeper = sleep or time.sleep
        self.interval = interval
        self.base_delay = base_delay
        self.cap_delay = cap_delay
        self.stable_after = stable_after
        self.log_max_bytes = log_max_bytes
        self.log_backups = log_backups
        self._children = [Child(spec) for spec in specs]
        self._stopping = False
        self._instance: SingleInstance | None = None
        self._job: WinJob | None = None
        self._own_log = RotatingLog(self.logs / "supervisor.log", max_bytes=log_max_bytes, backups=log_backups)
        self._job_warned = False
        self.started_at = iso_now()

    def run(self) -> int:
        try:
            return self._run()
        except Exception:
            message = "Супервизор упал:\n" + traceback.format_exc()
            try:
                self._log(message)
            except Exception:
                append_supervisor_log(message)
            return 1

    def _run(self) -> int:
        self._log(f"Автозапуск включается. Каталог {self.root}.")
        try:
            self._instance = SingleInstance(self.runtime / "supervisor.lock")
        except AlreadyRunning:
            self._log("Автозапуск уже работает, второй экземпляр закрывается.")
            return 0
        try:
            self._arm_signals()
            self._job = WinJob.try_create()
            self._instance.write_pid()
            write_owner("supervisor")
            self._clear_stop()
            self._log("Автозапуск включён.")
            while not self._stop_requested():
                try:
                    self.step()
                except Exception:
                    self._log("Сбой шага супервизора:\n" + traceback.format_exc())
                self._sleeper(self.interval)
            self._log("Останавливаюсь.")
            return 0
        finally:
            try:
                self.shutdown()
            except Exception:
                self._log("Остановка дочерних процессов сорвалась:\n" + traceback.format_exc())
            finally:
                self._clear_stop()
                clear_owner(os.getpid())
                if self._instance is not None:
                    self._instance.close()
                    self._instance = None
                if self._job is not None:
                    self._job.close()
                    self._job = None
                self._own_log.close()

    def step(self) -> None:
        now = self._clock()
        for child in self._children:
            self._reconcile(child, now)
        self._write_status()

    def shutdown(self) -> None:
        self._stopping = True
        for child in self._children:
            proc = child.proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.time() + 5
        for child in self._children:
            proc = child.proc
            if proc is None:
                continue
            remaining = max(0.1, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            self._reap(child)
            child.proc = None
            child.state = "stopped"
        self._write_status()

    def _reconcile(self, child: Child, now: float) -> None:
        proc = child.proc
        if proc is not None and proc.poll() is None:
            if now - child.started_mono >= self.stable_after:
                child.attempt = 0
            child.state = "running"
            return
        if proc is not None:
            code = proc.poll()
            self._reap(child)
            child.proc = None
            if self._stopping:
                child.state = "stopped"
                return
            child.restarts += 1
            child.last_exit = code
            child.attempt += 1
            delay = backoff_seconds(child.attempt, base=self.base_delay, cap=self.cap_delay)
            child.next_start = now + delay
            child.state = "backoff"
            detail = f"{child.spec.title} остановился с кодом {code}."
            if code == 3 and child.spec.name == "web":
                detail += " Порт сайта уже занят другим запуском python -m app."
            detail += f" Повтор через {delay:.0f} с."
            self._log(detail)
            return
        if self._stopping or now < child.next_start:
            if child.state != "stopped":
                child.state = "stopped" if self._stopping else "backoff"
            return
        self._spawn(child)

    def _spawn(self, child: Child) -> None:
        if child.log is None:
            child.log = RotatingLog(
                self.logs / f"{child.spec.name}.log",
                max_bytes=self.log_max_bytes,
                backups=self.log_backups,
            )
        try:
            proc = self._launch(child)
        except Exception as exc:
            child.attempt += 1
            delay = backoff_seconds(child.attempt, base=self.base_delay, cap=self.cap_delay)
            child.next_start = self._clock() + delay
            child.state = "backoff"
            self._log(f"{child.spec.title} не запустился: {exc}")
            return
        child.proc = proc
        child.state = "running"
        child.started_mono = self._clock()
        child.started_at = iso_now()
        child.pump = threading.Thread(
            target=self._pump,
            args=(proc, child.log),
            name=f"log-{child.spec.name}",
            daemon=True,
        )
        child.pump.start()
        if self._job is not None and proc.pid and not self._job.assign(proc.pid) and not self._job_warned:
            self._job_warned = True
            self._log("Не удалось привязать процесс к группе Windows. При жёстком завершении дочерние процессы закрою отдельно.")
        self._log(f"{child.spec.title} запущен, pid {proc.pid}.")

    def _popen(self, child: Child) -> subprocess.Popen:
        env = os.environ.copy()
        env.update(child.spec.env)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs: dict = dict(
            args=[self.executable, *child.spec.args],
            cwd=str(self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(**kwargs)

    def _pump(self, proc: subprocess.Popen, sink: RotatingLog) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                sink.write(str(line).rstrip("\r\n"))
        except Exception:
            return

    def _reap(self, child: Child) -> None:
        if child.pump is not None:
            child.pump.join(timeout=2)
            child.pump = None
        stream = getattr(child.proc, "stdout", None)
        if stream is not None and hasattr(stream, "close"):
            try:
                stream.close()
            except Exception:
                pass

    def _write_status(self) -> None:
        now = self._clock()
        children = {}
        for child in self._children:
            pid = child.proc.pid if child.state == "running" and child.proc is not None else None
            retry_at = None
            if child.state == "backoff":
                retry_at = time.time() + max(0, child.next_start - now)
            children[child.spec.name] = {
                "title": child.spec.title,
                "pid": pid,
                "state": child.state,
                "restarts": child.restarts,
                "last_exit": child.last_exit,
                "started_at": child.started_at,
                "retry_at": retry_at,
            }
        atomic_json(
            self.logs / "status.json",
            {
                "supervisor_pid": os.getpid(),
                "started_at": self.started_at,
                "updated_at": iso_now(),
                "children": children,
            },
        )

    def _stop_requested(self) -> bool:
        return self._stopping or (self.runtime / "stop.request").exists()

    def _clear_stop(self) -> None:
        try:
            (self.runtime / "stop.request").unlink(missing_ok=True)
        except OSError:
            pass

    def _arm_signals(self) -> None:
        def _request(_signum, _frame) -> None:
            try:
                (self.runtime / "stop.request").write_text("stop", encoding="ascii")
            except OSError:
                self._stopping = True

        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(name, _request)
            except (ValueError, OSError):
                pass

    def _log(self, text: str) -> None:
        self._own_log.write(f"{iso_now()} {text}")


def supervisor_is_running() -> bool:
    return lock_held(runtime_dir() / "supervisor.lock")


def print_status() -> int:
    running = supervisor_is_running()
    owner = read_owner()
    snapshot = read_json(logs_dir() / "status.json")
    heartbeats = {spec.name: read_heartbeat(spec.name) for spec in CHILD_SPECS}
    health = probe_health(local_site() + "/health") if running else (False, None, "")
    lines, code = format_status(
        owner_running=running,
        owner=owner,
        snapshot=snapshot,
        heartbeats=heartbeats,
        health=health,
        logs=logs_dir(),
    )
    print("\n".join(lines))
    return code


_APP_MODULE_RE = re.compile(
    r"(?:^|[\s\"])-m\s+app(?:\.(?:supervisor|runner|session_gate))?(?=\s|$)",
    re.IGNORECASE,
)


def _same_path(text: str, root: Path) -> bool:
    if not text:
        return False
    hay = text.replace("/", "\\").casefold()
    needle = str(root).replace("/", "\\").casefold()
    return needle in hay


def select_project_processes(
    processes: list[dict],
    *,
    root: Path,
    port_pids: set[int],
    own_pids: set[int],
) -> list[dict]:
    """Любой python -m app этого ящика: и из .venv, и системный, если он держит наш порт."""
    chosen: list[dict] = []
    for row in processes:
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in own_pids:
            continue
        command = str(row.get("command") or "")
        executable = str(row.get("executable") or "")
        if not _APP_MODULE_RE.search(command):
            continue
        if _same_path(command, root) or _same_path(executable, root) or pid in port_pids:
            chosen.append({"pid": pid, "command": command, "executable": executable})
    return chosen


def parse_netstat_listeners(text: str, port: int) -> list[int]:
    suffix = str(port)
    found: list[int] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[1]
        host, sep, listed = local.rpartition(":")
        if not sep or listed != suffix or not host:
            continue
        state = parts[3] if len(parts) >= 5 else ""
        if "LISTEN" not in state.upper() and "ПРОСЛУШ" not in state.upper() and "СЛУШ" not in state.upper():
            continue
        try:
            found.append(int(parts[-1]))
        except ValueError:
            continue
    return list(dict.fromkeys(found))


def _windows_processes() -> list[dict]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    raw = subprocess.check_output(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return _parse_process_json(raw)


def _parse_process_json(raw: str) -> list[dict]:
    import json

    raw = (raw or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    rows = data if isinstance(data, list) else [data]
    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed.append(
            {
                "pid": int(row.get("ProcessId") or 0),
                "command": str(row.get("CommandLine") or ""),
                "executable": str(row.get("ExecutablePath") or ""),
            }
        )
    return parsed


def _posix_processes() -> list[dict]:
    root = Path("/proc")
    if not root.exists():
        return []
    rows: list[dict] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            executable = os.readlink(entry / "exe")
        except OSError:
            continue
        rows.append({"pid": int(entry.name), "command": command, "executable": executable})
    return rows


def list_processes() -> list[dict]:
    try:
        if os.name == "nt":
            return _windows_processes()
        return _posix_processes()
    except Exception as exc:
        print(f"Не смог прочитать список процессов: {exc}")
        return []


def _windows_listeners(port: int) -> list[int]:
    raw = subprocess.check_output(
        ["netstat", "-ano", "-p", "tcp"],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    return parse_netstat_listeners(raw, port)


def _can_bind(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((settings.host or "127.0.0.1", port))
        except OSError:
            return False
    return True


def port_owners(port: int) -> list[int] | None:
    """None — порт свободен. Пустой список — занят, но хозяина не видно."""
    try:
        if os.name == "nt":
            found = _windows_listeners(port)
        else:
            found = []
            if not _can_bind(port):
                return []
    except Exception:
        return None if _can_bind(port) else []
    if os.name == "nt":
        return found or None
    return None


def terminate_pid(pid: int) -> None:
    """Завершить процесс. На Windows не через os.kill: тот же сигнал 0 роняет интерпретатор."""
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        _windows_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, SystemError, ProcessLookupError):
        return


def _windows_terminate(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _release_supervisor_lock() -> None:
    path = runtime_dir() / "supervisor.lock"
    if not lock_held(path):
        return
    owner = read_owner() or {}
    pid = int(owner.get("pid") or 0)
    kind = str(owner.get("kind") or "")
    if kind == "manual":
        print("Сейчас ящик запущен вручную. Останавливаю этот процесс.")
        if pid:
            terminate_pid(pid)
    else:
        (runtime_dir() / "stop.request").write_text("stop", encoding="ascii")
        for _ in range(50):
            if not lock_held(path):
                break
            time.sleep(0.2)
        if lock_held(path) and pid:
            terminate_pid(pid)
    for _ in range(25):
        if not lock_held(path):
            return
        time.sleep(0.2)


def _command_for(pid: int, processes: list[dict]) -> str:
    for row in processes:
        try:
            if int(row.get("pid") or 0) == pid:
                return str(row.get("command") or row.get("executable") or "")
        except (TypeError, ValueError):
            continue
    return ""


def stop_supervisor() -> int:
    """Гасит супервизор и любой python -m app этого проекта, каким бы интерпретатором его ни запустили."""
    _release_supervisor_lock()
    processes = list_processes()
    owners = port_owners(settings.port)
    port_pids = set(owners or [])
    targets = select_project_processes(
        processes,
        root=ROOT,
        port_pids=port_pids,
        own_pids={os.getpid()},
    )
    for row in targets:
        print(f"Останавливаю прежний процесс {row['pid']}: {row['command']}")
        terminate_pid(row["pid"])
    if targets:
        for _ in range(15):
            owners = port_owners(settings.port)
            if not owners:
                break
            time.sleep(0.2)
    if lock_held(runtime_dir() / "supervisor.lock"):
        print("Замок автозапуска всё ещё занят. Закрой окно python -m app и запусти установку ещё раз.")
        return 1
    if owners is None:
        print("Прежний запуск остановлен.")
        return 0
    if not owners:
        print(
            f"Порт {settings.port} занят, но не удалось увидеть, кем. "
            "Закрой окно, где запущен python -m app, и запусти установку ещё раз."
        )
        return 1
    lines = []
    for pid in owners:
        if pid == os.getpid():
            continue
        command = _command_for(pid, processes) or "команду прочитать не удалось"
        lines.append(f"Порт {settings.port} занят процессом {pid}: {command}")
    if not lines:
        print("Прежний запуск остановлен.")
        return 0
    print("\n".join(lines))
    print("Этот процесс не удалось остановить. Закрой его и запусти установку ещё раз. Иначе сайт будет падать: порт занят.")
    return 1


def append_supervisor_log(text: str) -> None:
    try:
        path = logs_dir() / "supervisor.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = text if text.endswith("\n") else text + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{iso_now()} {line}")
            handle.flush()
    except Exception:
        return


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except Exception:
        append_supervisor_log("Супервизор упал до цикла:\n" + traceback.format_exc())
        return 1


def _dispatch(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "run"
    if command == "run":
        return Supervisor().run()
    if command == "status":
        return print_status()
    if command == "stop":
        return stop_supervisor()
    print("Команды: run, status, stop", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
