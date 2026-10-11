"""Автозапуск: замок, повтор после падения, сеть, сессия Telegram, тексты проверки."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.servicekit import (
    AlreadyRunning,
    RotatingLog,
    SingleInstance,
    backoff_seconds,
    embed_workers,
    logs_dir,
    peek_telegram_authorized,
    pid_alive,
    read_json,
    runtime_dir,
    wait_for_network,
)
from app.settings import ROOT, settings
from app.supervisor import CHILD_SPECS, ChildSpec, Supervisor, format_status, print_status

HOLD_LOCK = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from app.servicekit import SingleInstance
    lock = SingleInstance(Path(sys.argv[1]))
    lock.write_pid()
    print("ready", flush=True)
    sys.stdin.readline()
    """
)


class FakeProc:
    def __init__(self, *, code: int = 1, die_on_poll: int | None = 1):
        self.pid = 20000 + (id(self) % 10000)
        self.code = code
        self._die = die_on_poll
        self._polls = 0
        self.returncode = None
        self.stdout = iter(())
        self.killed = False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.killed:
            self.returncode = -9
            return self.returncode
        self._polls += 1
        if self._die is not None and self._polls >= self._die:
            self.returncode = self.code
            return self.code
        return None

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        result = self.poll()
        if result is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return result


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LEADS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("LEADS_ROLE", raising=False)


def test_backoff_grows_and_caps():
    assert backoff_seconds(1, base=1, cap=60) == 1
    assert backoff_seconds(2, base=1, cap=60) == 2
    assert backoff_seconds(3, base=1, cap=60) == 4
    assert backoff_seconds(10, base=1, cap=60) == 60


def test_manual_start_embeds_workers_supervised_web_does_not(monkeypatch):
    monkeypatch.delenv("LEADS_ROLE", raising=False)
    assert embed_workers() is True
    monkeypatch.setenv("LEADS_ROLE", "web")
    assert embed_workers() is False
    monkeypatch.setenv("LEADS_ROLE", "telegram")
    assert embed_workers() is False


def test_web_process_does_not_start_collectors(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("LEADS_ROLE", "web")
    monkeypatch.setattr(settings, "db_path", tmp_path / "web.db")
    from app import boards, networking, notifier

    called: list[str] = []

    async def boards_poll():
        called.append("boards")

    async def net_poll():
        called.append("networking")

    async def note_start():
        called.append("notifier")
        return False

    monkeypatch.setattr(boards, "poll_forever", boards_poll)
    monkeypatch.setattr(networking, "poll_forever", net_poll)
    monkeypatch.setattr(notifier, "start", note_start)
    monkeypatch.setattr(notifier, "stop", note_start)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        pass
    assert called == []


def test_peek_telegram_authorized_uses_session_file(tmp_path, monkeypatch):
    session = tmp_path / "telegram"
    monkeypatch.setattr(settings, "session_path", session)
    assert peek_telegram_authorized() is False
    Path(f"{session}.session").write_bytes(b"session")
    assert peek_telegram_authorized() is True


def test_log_rotates_and_keeps_unicode(tmp_path):
    path = tmp_path / "web.log"
    sink = RotatingLog(path, max_bytes=80, backups=2)
    sink.write("Павел " + ("я" * 40))
    sink.write("ещё " + ("б" * 40))
    sink.close()
    archived = tmp_path / "web.log.1"
    assert archived.exists()
    assert "Павел" in archived.read_text(encoding="utf-8")


def test_wait_for_network_backs_off_until_probe_passes():
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return calls["n"] >= 3

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    asyncio.run(wait_for_network(probe=probe, sleep=fake_sleep, base=2, cap=30))
    assert delays == [2, 4]
    assert calls["n"] == 3


def test_second_process_cannot_take_the_lock(tmp_path):
    path = tmp_path / "supervisor.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(path)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        with pytest.raises(AlreadyRunning):
            SingleInstance(path)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            SingleInstance(path).close()
            return
        except AlreadyRunning:
            time.sleep(0.05)
    raise AssertionError("замок не освободился после остановки процесса")


def test_session_borrow_waits_until_owner_yields(tmp_path):
    from app.session_gate import borrow_session

    runtime = tmp_path / "gate"
    runtime.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.session_gate", "hold", str(runtime)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"

        async def take():
            async with borrow_session(runtime, timeout=5):
                assert (runtime / "session.yielded").exists() or not (runtime / "session.owner").exists()

        asyncio.run(take())
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    assert proc.returncode == 0


def test_crashed_child_restarts_after_backoff():
    launched: list[FakeProc] = []

    def launch(_child):
        proc = FakeProc(code=3, die_on_poll=1)
        launched.append(proc)
        return proc

    now = {"t": 0.0}
    sup = Supervisor(
        specs=(ChildSpec("demo", "Демо", ["-c", "pass"], {}),),
        launch=launch,
        clock=lambda: now["t"],
        sleep=lambda _seconds: None,
        base_delay=2,
        cap_delay=8,
        stable_after=30,
    )
    sup.step()
    assert len(launched) == 1
    assert sup._children[0].state == "running"
    sup.step()
    child = sup._children[0]
    assert child.state == "backoff"
    assert child.restarts == 1
    assert child.last_exit == 3
    assert child.next_start == 2
    sup.step()
    assert len(launched) == 1
    now["t"] = 2
    sup.step()
    assert len(launched) == 2
    text = (logs_dir() / "supervisor.log").read_text(encoding="utf-8")
    assert "остановился с кодом 3" in text
    sup.shutdown()


def test_stable_child_resets_backoff():
    def launch(_child):
        return FakeProc(die_on_poll=None)

    now = {"t": 0.0}
    sup = Supervisor(
        specs=(ChildSpec("demo", "Демо", ["-c", "pass"], {}),),
        launch=launch,
        clock=lambda: now["t"],
        sleep=lambda _seconds: None,
        stable_after=30,
    )
    sup.step()
    sup._children[0].attempt = 4
    now["t"] = 31
    sup.step()
    assert sup._children[0].attempt == 0
    assert sup._children[0].state == "running"
    sup.shutdown()


def test_supervisor_runs_child_writes_log_and_stops():
    spec = ChildSpec(
        "demo",
        "Демо",
        ["-c", "import time; print('up', flush=True); time.sleep(30)"],
        {},
    )
    sup = Supervisor(
        specs=(spec,),
        root=runtime_dir().parent,
        interval=0.05,
        base_delay=0.05,
        cap_delay=0.2,
        stable_after=10,
    )
    thread = threading.Thread(target=sup.run, daemon=True)
    thread.start()
    pid = None
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot = read_json(logs_dir() / "status.json") or {}
            row = (snapshot.get("children") or {}).get("demo") or {}
            log_path = logs_dir() / "demo.log"
            if row.get("state") == "running" and row.get("pid") and log_path.exists() and "up" in log_path.read_text(encoding="utf-8"):
                pid = int(row["pid"])
                break
            time.sleep(0.05)
        assert pid
        assert pid_alive(pid)
        (runtime_dir() / "stop.request").write_text("stop", encoding="ascii")
        thread.join(5)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            (runtime_dir() / "stop.request").write_text("stop", encoding="ascii")
            thread.join(5)
    assert pid is not None
    assert not pid_alive(pid)


def test_status_text_when_autostart_is_down(capsys):
    assert print_status() == 1
    text = capsys.readouterr().out
    assert "Автозапуск не запущен." in text
    assert "install-autostart.ps1" in text


def test_status_text_lists_every_process_and_the_page(tmp_path):
    snapshot = {
        "children": {
            spec.name: {"state": "running", "pid": 100 + index, "restarts": 0, "last_exit": None}
            for index, spec in enumerate(CHILD_SPECS)
        }
    }
    heartbeats = {spec.name: {"detail": "Живая лента включена."} for spec in CHILD_SPECS}
    lines, code = format_status(
        owner_running=True,
        owner={"kind": "supervisor", "pid": 42},
        snapshot=snapshot,
        heartbeats=heartbeats,
        health=(True, {"ok": True, "authorized": True, "telegram_api": True, "bot": True}, ""),
        logs=tmp_path,
        alive=lambda _pid: True,
    )
    assert code == 0
    text = "\n".join(lines)
    for spec in CHILD_SPECS:
        assert spec.title in text
    assert "Страница открывается:" in text
    assert "Вход в Telegram есть." in text

    snapshot["children"]["notifier"]["state"] = "backoff"
    snapshot["children"]["notifier"]["retry_at"] = time.time() + 5
    snapshot["children"]["notifier"]["last_exit"] = 1
    lines, code = format_status(
        owner_running=True,
        owner={"kind": "supervisor", "pid": 42},
        snapshot=snapshot,
        heartbeats=heartbeats,
        health=(True, {"ok": True, "authorized": False, "telegram_api": True, "bot": False}, ""),
        logs=tmp_path,
        alive=lambda _pid: True,
    )
    assert code == 1
    assert any("Бот сейчас перезапускается" in line for line in lines)


def test_profile_request_returns_listener_answer(tmp_path):
    from app.collector import _profile_via_listener

    runtime = tmp_path / "runtime"
    runtime.mkdir()

    async def go():
        async def reply():
            for _ in range(50):
                request = runtime / "profile.request.json"
                if request.exists():
                    payload = json.loads(request.read_text(encoding="utf-8"))
                    (runtime / "profile.result.json").write_text(
                        json.dumps({"token": payload["token"], "about": "магазин аккумуляторов", "kind": "user"}),
                        encoding="utf-8",
                    )
                    return
                await asyncio.sleep(0.02)

        waiter = asyncio.create_task(reply())
        result = await _profile_via_listener(runtime, {"sender_username": "@shop"})
        await waiter
        return result

    assert asyncio.run(go()) == {"about": "магазин аккумуляторов", "kind": "user"}


def test_entrypoints_and_windows_scripts():
    names = [spec.name for spec in CHILD_SPECS]
    assert names == ["web", "telegram", "boards", "networking", "notifier"]
    bot = next(spec for spec in CHILD_SPECS if spec.name == "notifier")
    assert bot.args == ["-m", "app.runner", "notifier"]
    assert bot.env == {"LEADS_ROLE": "notifier"}
    assert (ROOT / "app" / "__main__.py").exists()
    assert (ROOT / "app" / "runner.py").exists()
    vbs = (ROOT / "scripts" / "start-hidden.vbs").read_bytes()
    assert b"app.supervisor run" in vbs
    assert b", 0, True" in vbs
    vbs.decode("ascii")
    install = (ROOT / "scripts" / "install-autostart.ps1").read_bytes()
    assert install.startswith(b"\xef\xbb\xbf")
    text = install.decode("utf-8-sig")
    assert "PavelLeadsInbox" in text
    assert "AtLogOn" in text
    assert "RestartCount 999" in text
    assert "ExecutionTimeLimit" in text
    assert "MultipleInstances IgnoreNew" in text
    assert "RunLevel Limited" in text
    assert "LiteralPath" in text
    assert "start-hidden.vbs" in text
    uninstall = (ROOT / "scripts" / "uninstall-autostart.ps1").read_text(encoding="utf-8-sig")
    status = (ROOT / "scripts" / "status.ps1").read_text(encoding="utf-8-sig")
    assert "PavelLeadsInbox" in uninstall
    assert "app.supervisor stop" in uninstall
    assert "app.supervisor status" in status
    assert "LiteralPath" in status
