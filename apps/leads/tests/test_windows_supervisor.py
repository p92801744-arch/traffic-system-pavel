"""Windows: живой процесс без os.kill, остановка чужого python -m app, журнал падения."""

from __future__ import annotations

import inspect

import pytest

from app import servicekit, supervisor
from app.servicekit import interpret_windows_liveness, pid_alive
from app.supervisor import (
    ChildSpec,
    Supervisor,
    append_supervisor_log,
    main,
    parse_netstat_listeners,
    select_project_processes,
    stop_supervisor,
)
from app.servicekit import logs_dir


ROOT = __import__("pathlib").Path(r"C:\Users\Павел\Desktop\Pavel\apps\leads")
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
GLOBAL = r"C:\Users\Павел\AppData\Local\Programs\Python\Python311\python.exe"


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LEADS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("LEADS_ROLE", raising=False)


def test_windows_liveness_uses_exit_code():
    assert interpret_windows_liveness(True, 0, 259) is True
    assert interpret_windows_liveness(True, 0, 0) is False
    assert interpret_windows_liveness(False, 87, None) is False
    assert interpret_windows_liveness(False, 5, None) is True
    assert interpret_windows_liveness(True, 5, None) is True


def test_pid_alive_on_windows_does_not_call_os_kill(monkeypatch):
    monkeypatch.setattr(servicekit.os, "name", "nt")

    def boom(*_args, **_kwargs):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    monkeypatch.setattr(servicekit.os, "kill", boom)
    monkeypatch.setattr(servicekit, "_query_windows_process", lambda pid: (True, 0, 259) if pid == 7 else (False, 87, None))
    assert pid_alive(7) is True
    assert pid_alive(8) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_hides_a_broken_probe(monkeypatch):
    monkeypatch.setattr(servicekit.os, "name", "nt")

    def boom(_pid):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    monkeypatch.setattr(servicekit, "_windows_pid_alive", boom)
    assert pid_alive(42) is False


def test_lock_does_not_use_kill():
    source = "\n".join(
        inspect.getsource(item)
        for item in (
            servicekit.SingleInstance,
            servicekit.lock_held,
            servicekit._lock_nb,
            servicekit._unlock,
            servicekit._open_lock_file,
        )
    )
    assert "os.kill" not in source
    assert "pid_alive" not in source


def test_posix_probe_does_not_raise_systemerror(monkeypatch):
    monkeypatch.setattr(servicekit.os, "name", "posix")

    def boom(*_args, **_kwargs):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    monkeypatch.setattr(servicekit.os, "kill", boom)
    assert pid_alive(123) is False


def test_global_python_on_our_port_is_ours():
    processes = [
        {"pid": 10, "command": f'"{GLOBAL}" -m app', "executable": GLOBAL},
        {"pid": 11, "command": f'"{GLOBAL}" -m app', "executable": GLOBAL},
        {"pid": 12, "command": r"C:\Python311\python.exe -m http.server 8787", "executable": r"C:\Python311\python.exe"},
    ]
    chosen = select_project_processes(processes, root=ROOT, port_pids={10}, own_pids={99})
    assert [row["pid"] for row in chosen] == [10]


def test_venv_runner_is_ours_without_the_port():
    command = f'"{VENV}" -m app.runner telegram'
    chosen = select_project_processes(
        [{"pid": 4, "command": command, "executable": str(VENV)}],
        root=ROOT,
        port_pids=set(),
        own_pids=set(),
    )
    assert [row["pid"] for row in chosen] == [4]


def test_other_project_and_self_are_left_alone():
    processes = [
        {"pid": 5, "command": r"D:\other\python.exe -m app", "executable": r"D:\other\python.exe"},
        {"pid": 6, "command": f'"{VENV}" -m app.supervisor stop', "executable": str(VENV)},
    ]
    chosen = select_project_processes(processes, root=ROOT, port_pids=set(), own_pids={6})
    assert chosen == []


def test_netstat_finds_the_listener():
    text = """
  TCP    127.0.0.1:8787         0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:87870        0.0.0.0:0              LISTENING       7
  TCP    127.0.0.1:80           0.0.0.0:0              LISTENING       4
  TCP    [::1]:8787             [::]:0                 LISTENING       4242
"""
    assert parse_netstat_listeners(text, 8787) == [4242]


def test_stop_kills_global_python_holding_the_port(monkeypatch, capsys):
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        supervisor,
        "list_processes",
        lambda: [{"pid": 4242, "command": f'"{GLOBAL}" -m app', "executable": GLOBAL}],
    )
    calls = {"n": 0}

    def owners(_port):
        calls["n"] += 1
        return [4242] if calls["n"] == 1 else None

    monkeypatch.setattr(supervisor, "port_owners", owners)
    killed: list[int] = []
    monkeypatch.setattr(supervisor, "terminate_pid", killed.append)
    assert stop_supervisor() == 0
    assert killed == [4242]
    assert "Python311" in capsys.readouterr().out


def test_stop_reports_a_foreign_listener(monkeypatch, capsys):
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        supervisor,
        "list_processes",
        lambda: [{"pid": 50, "command": r"C:\nginx\nginx.exe", "executable": r"C:\nginx\nginx.exe"}],
    )
    monkeypatch.setattr(supervisor, "port_owners", lambda _port: [50])
    monkeypatch.setattr(supervisor, "terminate_pid", lambda _pid: (_ for _ in ()).throw(AssertionError("чужой процесс")))
    assert stop_supervisor() == 1
    text = capsys.readouterr().out
    assert "8787" in text
    assert "nginx" in text


def test_stop_reports_a_busy_port_without_an_owner(monkeypatch, capsys):
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(supervisor, "list_processes", lambda: [])
    monkeypatch.setattr(supervisor, "port_owners", lambda _port: [])
    assert stop_supervisor() == 1
    assert "8787" in capsys.readouterr().out


class _ExitProc:
    def __init__(self):
        self.pid = 321
        self.returncode = None
        self._polls = 0
        self.stdout = iter(())

    def poll(self):
        self._polls += 1
        if self._polls >= 1 and self.returncode is None:
            self.returncode = 3
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_web_exit_3_names_the_taken_port():
    def launch(_child):
        return _ExitProc()

    now = {"t": 0.0}
    sup = Supervisor(
        specs=(ChildSpec("web", "Сайт", ["-m", "app"], {"LEADS_ROLE": "web"}),),
        launch=launch,
        clock=lambda: now["t"],
        sleep=lambda _seconds: None,
        base_delay=2,
        cap_delay=8,
        stable_after=30,
    )
    sup.step()
    sup.step()
    text = (logs_dir() / "supervisor.log").read_text(encoding="utf-8")
    assert "остановился с кодом 3" in text
    assert "порт" in text.lower()
    sup.shutdown()


def test_supervisor_crash_is_written_and_returns_nonzero():
    sup = Supervisor(specs=(), sleep=lambda _seconds: None, interval=0.01)

    def boom():
        raise SystemError("<built-in function kill> returned a result with an exception set")

    sup._arm_signals = boom
    assert sup.run() == 1
    text = (logs_dir() / "supervisor.log").read_text(encoding="utf-8")
    assert "SystemError" in text
    assert "returned a result with an exception set" in text


def test_main_logs_when_the_supervisor_cannot_even_start(monkeypatch):
    class Boom(Supervisor):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("нет каталога журнала")

    monkeypatch.setattr(supervisor, "Supervisor", Boom)
    assert main(["run"]) == 1
    text = (logs_dir() / "supervisor.log").read_text(encoding="utf-8")
    assert "нет каталога журнала" in text
    append_supervisor_log("ещё строка")
    assert "ещё строка" in (logs_dir() / "supervisor.log").read_text(encoding="utf-8")
