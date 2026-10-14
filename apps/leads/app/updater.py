"""Кнопка «Обновить ящик»: git fetch/pull --ff-only и безопасный перезапуск.

Панель сама проверяет обновление и делает pull (быстро). Перезапуск делает отдельный
процесс `python -m app.updater restart`: он переживает остановку панели, гасит прежние
процессы ящика (кроме себя) и снова запускает задачу PavelLeadsInbox.
Никогда не делает force/reset: при локальных правках или конфликте pull отказывается.
Путь перезапуска проверен вживую 2026-10-14 (через WMI).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from app.settings import ROOT

log = logging.getLogger("leads.updater")

TASK_NAME = "PavelLeadsInbox"
BRANCH = "master"
BREAKAWAY = 0x01000000


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )


def check() -> dict:
    """{'state': 'none'|'ahead'|'error', 'behind': N, 'message': ...}"""
    try:
        fetched = _git("fetch", "--quiet", "origin", BRANCH, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "error", "behind": 0, "message": f"git fetch не прошёл: {exc}"}
    if fetched.returncode != 0:
        return {"state": "error", "behind": 0, "message": "git fetch не прошёл: " + (fetched.stderr or "").strip()[:300]}
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != BRANCH:
        return {"state": "error", "behind": 0, "message": f"Сейчас ветка {branch}, обновляю только {BRANCH}."}
    counted = _git("rev-list", "--count", f"HEAD..origin/{BRANCH}")
    try:
        behind = int(counted.stdout.strip() or 0)
    except ValueError:
        behind = 0
    return {"state": "ahead" if behind else "none", "behind": behind, "message": ""}


def pull() -> dict:
    """git pull --ff-only. {'ok': bool, 'message': str, 'requirements': bool}"""
    before = _git("rev-parse", "HEAD").stdout.strip()
    result = _git("pull", "--ff-only", "origin", BRANCH, timeout=120)
    if result.returncode != 0:
        text = ((result.stderr or "") + (result.stdout or "")).strip()
        return {
            "ok": False,
            "requirements": False,
            "message": "Обновление не применилось (локальные правки или конфликт, принудительно не трогаю): " + text[:400],
        }
    changed = _git("diff", "--name-only", before, "HEAD").stdout.split()
    needs = any(name.endswith("requirements.txt") for name in changed)
    return {"ok": True, "requirements": needs, "message": f"Обновлено, файлов изменено: {len(changed)}"}


def spawn_restart(requirements: bool) -> None:
    """Запускает отдельный процесс перезапуска, который не умирает вместе с панелью."""
    args = [sys.executable, "-m", "app.updater", "restart"]
    if requirements:
        args.append("--pip")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | BREAKAWAY  # DETACHED | NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
    kwargs = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt" and _spawn_outside_job(args):
        return
    try:
        subprocess.Popen(args, creationflags=flags, **kwargs)
    except OSError:
        subprocess.Popen(args, creationflags=flags & ~BREAKAWAY, **kwargs)


def _spawn_outside_job(args: list[str]) -> bool:
    """Запуск через WMI: процесс рождается вне задачи планировщика,
    поэтому Stop-ScheduledTask его не убивает (флаг breakaway задача не разрешает)."""
    line = subprocess.list2cmdline(args).replace("'", "''")
    folder = str(ROOT).replace("'", "''")
    command = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{line}'; CurrentDirectory='{folder}'}}; exit $r.ReturnValue"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _log(text: str) -> None:
    from app.supervisor import append_supervisor_log

    append_supervisor_log("Обновление: " + text)


def _ps(command: str) -> int:
    return subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True).returncode


def restart(pip: bool = False) -> int:
    _log("перезапуск начат, pid {}".format(os.getpid()))
    time.sleep(2)  # дать панели отдать страницу «обновляюсь»
    if pip:
        done = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                              cwd=str(ROOT))
        _log(f"pip install, код {done.returncode}")
    from app.supervisor import stop_supervisor

    if os.name == "nt":
        _ps(f"Stop-ScheduledTask -TaskName {TASK_NAME}")
    code = stop_supervisor(keep_pids={os.getpid()})
    _log(f"прежние процессы остановлены, код {code}")
    time.sleep(2)
    if os.name != "nt":
        return 0
    started = _ps(f"Start-ScheduledTask -TaskName {TASK_NAME}")
    _log(f"задача {TASK_NAME} запущена, код {started}")
    return started


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "restart":
        return restart(pip="--pip" in args)
    print(check())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())