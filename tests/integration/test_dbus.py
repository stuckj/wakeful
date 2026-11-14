"""wakeful against stand-in services on a private session bus.

Run with:  dbus-run-session -- python3 -m pytest -m dbus

These start the real program as a subprocess and read back, from the stub,
which inhibitions it holds and how each one ended: released by UnInhibit, or
dropped because the holder's connection closed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jeepney import DBusAddress, MessageType, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

pytestmark = pytest.mark.dbus

HERE = Path(__file__).parent
SRC = HERE.parent.parent / "src"
APP_BUS_NAME = "io.github.stuckj.Wakeful"
# WAKEFUL_CMD=wakeful tests an installed package instead of the source tree.
INSTALLED = os.environ.get("WAKEFUL_CMD")
COMMAND = INSTALLED.split() if INSTALLED else [sys.executable, "-m", "wakeful"]


def wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Stub:
    def __init__(self, *args: str) -> None:
        self.args = args
        self.proc: subprocess.Popen[str] | None = None
        self.conn = open_dbus_connection(bus="SESSION")

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "stub_services.py"), *self.args],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if line.strip() != "ready":
            self.proc.kill()
            pytest.fail("stub services did not start; is this a private bus (dbus-run-session)?")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def dump(self) -> dict:
        addr = DBusAddress("/test/wakeful/Stub", "test.wakeful.Stub", "test.wakeful.Stub")
        reply = self.conn.send_and_get_reply(new_method_call(addr, "Dump"))
        return json.loads(reply.body[0])

    def held(self, kind: str | None = None) -> list[dict]:
        return [h for h in self.dump()["held"] if kind is None or h["kind"] == kind]

    def owner(self, name: str) -> str | None:
        reply = self.conn.send_and_get_reply(message_bus.GetNameOwner(name))
        if reply.header.message_type != MessageType.method_return:
            return None
        return reply.body[0]


@pytest.fixture
def stub():
    s = Stub()
    s.start()
    yield s
    s.stop()
    s.conn.close()


@pytest.fixture
def env(tmp_path):
    e = dict(os.environ)
    if not INSTALLED:
        e["PYTHONPATH"] = str(SRC)
    e.update(
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        CLAUDE_CONFIG_DIR=str(tmp_path / "claude"),
        QT_QPA_PLATFORM="offscreen",
    )
    return e


class App:
    def __init__(self, env: dict[str, str], *args: str) -> None:
        self.env = env
        self.proc = subprocess.Popen(
            [*COMMAND, "-v", *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*COMMAND, *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def stop(self) -> tuple[str, str]:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            out, err = self.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        return out, err


@pytest.fixture
def app(stub, env):
    apps: list[App] = []

    def launch(*args: str) -> App:
        a = App(env, *args)
        apps.append(a)
        wait_for(lambda: stub.owner(APP_BUS_NAME), what="wakeful to claim its bus name")
        return a

    yield launch
    for a in apps:
        _, err = a.stop()
        assert "Traceback" not in err, err


def kconfig(env, *args: str) -> str:
    return subprocess.run(
        list(args), env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def lid_value(env, profile: str) -> str:
    return kconfig(
        env,
        "kreadconfig6",
        "--file",
        "powerdevilrc",
        "--group",
        profile,
        "--group",
        "SuspendAndShutdown",
        "--key",
        "LidAction",
        "--default",
        "unset",
    )


def set_lid(env, profile: str, value: str | None) -> None:
    args = [
        "kwriteconfig6",
        "--file",
        "powerdevilrc",
        "--group",
        profile,
        "--group",
        "SuspendAndShutdown",
        "--key",
        "LidAction",
    ]
    kconfig(env, *args, *(["--delete"] if value is None else [value]))


# -- the block itself -------------------------------------------------------


def test_sleep_block_is_held_by_the_app_and_released_on_stop(stub, app):
    a = app("--no-tray")
    r = a.cli("start", "47m")
    assert r.returncode == 0, r.stderr
    assert "47 min left" in r.stdout

    [held] = stub.held()
    assert held["kind"] == "pm"
    assert held["who"] == "wakeful"
    assert held["why"] == "Keeping awake: 47 min left"
    assert held["sender"] == stub.owner(APP_BUS_NAME)

    assert a.cli("stop").returncode == 0
    assert stub.held() == []
    assert ["uninhibit", "pm", 1, True] in stub.dump()["events"]


def test_screen_mode_takes_both_inhibitions(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "--screen", "1h").returncode == 0
    assert sorted(h["kind"] for h in stub.held()) == ["pm", "ss"]
    assert a.cli("stop").returncode == 0
    assert stub.held() == []


def test_expiry_releases_and_the_app_keeps_running(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "--screen", "2s").returncode == 0
    assert len(stub.held()) == 2
    wait_for(lambda: stub.held() == [], timeout=10, what="the block to expire")
    events = stub.dump()["events"]
    assert ["uninhibit", "pm", 1, True] in events
    assert ["uninhibit", "ss", 1, True] in events
    assert a.proc.poll() is None
    assert "Not keeping awake" in a.cli("status").stdout


def test_until_off_block_does_not_expire(stub, app):
    a = app("--no-tray")
    assert a.cli("start").returncode == 0
    assert "until turned off" in a.cli("status").stdout
    time.sleep(6)  # longer than the longest tick
    assert len(stub.held()) == 1


def test_extend_updates_the_reason(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "30m").returncode == 0
    assert a.cli("extend", "1h").returncode == 0
    reasons = [h["why"] for h in stub.held("pm")]
    assert "Keeping awake: 1 h 30 min left" in reasons


def test_kill_9_releases_through_the_connection_closing(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "--screen").returncode == 0
    assert len(stub.held()) == 2
    a.proc.kill()
    a.proc.wait()
    wait_for(lambda: stub.held() == [], what="the inhibitions to be dropped")
    kinds = sorted(e[1] for e in stub.dump()["events"] if e[0] == "disconnect")
    assert kinds == ["pm", "ss"]


def test_sigterm_releases_explicitly(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "--screen", "1h").returncode == 0
    a.proc.send_signal(signal.SIGTERM)
    assert a.proc.wait(timeout=10) == 0
    events = stub.dump()["events"]
    assert ["uninhibit", "pm", 1, True] in events
    assert ["uninhibit", "ss", 1, True] in events
    assert stub.held() == []


def test_second_instance_exits_and_leaves_the_first(stub, app, env):
    first = app("--no-tray")
    second = subprocess.run(
        [*COMMAND, "--no-tray"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 0
    assert "already running" in second.stderr
    assert first.proc.poll() is None


def test_mode_switch_never_leaves_a_gap(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "--screen", "1h").returncode == 0
    assert call_app(stub, "SetMode", "s", "sleep")["ok"]

    # The new PowerManagement inhibition is taken before the old ones go.
    assert len(stub.held("pm")) == 2
    assert len(stub.held("ss")) == 1
    wait_for(
        lambda: len(stub.held("pm")) == 1 and not stub.held("ss"),
        timeout=12,
        what="the replaced inhibitions to be retired",
    )


def test_service_restart_is_followed(stub, app):
    a = app("--no-tray")
    assert a.cli("start", "1h").returncode == 0
    stub.stop()
    stub.start()  # a new PowerDevil that knows nothing of the old cookies
    wait_for(lambda: stub.held("pm"), what="the inhibition to be re-taken")
    assert stub.held("pm")[0]["who"] == "wakeful"
    assert a.cli("stop").returncode == 0
    assert stub.held() == []


def test_slow_service_restart_is_retried_until_it_answers(stub, app):
    a = app("--no-tray")
    assert a.cli("start").returncode == 0  # until turned off: the reason never changes
    stub.stop()
    stub.args = ("--fail-inhibits=5",)
    stub.start()
    wait_for(lambda: stub.held("pm"), timeout=40, what="the inhibition to be re-taken")
    assert len([e for e in stub.dump()["events"] if e[0] == "refused"]) == 5
    assert "Keeping awake" in a.cli("status").stdout


def test_start_fails_cleanly_when_a_service_is_missing(env):
    s = Stub("--no-screensaver")
    s.start()
    a = App(env, "--no-tray")
    try:
        wait_for(lambda: s.owner(APP_BUS_NAME), what="wakeful to start")
        r = a.cli("start", "--screen", "1h")
        assert r.returncode == 1
        assert "org.freedesktop.ScreenSaver" in r.stderr
        assert s.held() == []
        assert "Not keeping awake" in a.cli("status").stdout

        # A failed restart leaves the running block as it was.
        assert a.cli("start", "1h").returncode == 0
        assert a.cli("start", "--screen", "2h").returncode == 1
        assert "1 h left" in a.cli("status").stdout
        assert [h["why"] for h in s.held()] == ["Keeping awake: 1 h left"]
    finally:
        a.stop()
        s.stop()


def test_tray_runs_headless(stub, app):
    a = app()
    assert a.cli("start", "30m").returncode == 0
    assert len(stub.held()) == 1
    assert a.cli("stop").returncode == 0


# -- the lid option ---------------------------------------------------------


def call_app(stub, method: str, sig: str = "", *args) -> dict:
    addr = DBusAddress("/io/github/stuckj/Wakeful", APP_BUS_NAME, APP_BUS_NAME)
    reply = stub.conn.send_and_get_reply(new_method_call(addr, method, sig, args))
    return json.loads(reply.body[0])


def test_lid_ignored_during_block_and_restored(stub, app, env):
    set_lid(env, "AC", "1")
    set_lid(env, "LowBattery", "32")
    a = app("--no-tray")
    assert call_app(stub, "SetIgnoreLid", "b", True)["ok"]
    assert lid_value(env, "AC") == "1"  # nothing changes until a block runs

    assert a.cli("start", "1h").returncode == 0
    assert [lid_value(env, p) for p in ("AC", "Battery", "LowBattery")] == ["0", "0", "0"]
    assert "lid will not sleep" in a.cli("status").stdout
    assert stub.dump()["refreshes"] == 1

    assert a.cli("stop").returncode == 0
    assert [lid_value(env, p) for p in ("AC", "Battery", "LowBattery")] == ["1", "unset", "32"]
    assert stub.dump()["refreshes"] == 2
    assert not (Path(env["XDG_STATE_HOME"]) / "wakeful" / "lid-restore.json").exists()


def test_second_instance_leaves_the_running_lid_change_alone(stub, app, env):
    set_lid(env, "AC", "1")
    a = app("--no-tray")
    assert call_app(stub, "SetIgnoreLid", "b", True)["ok"]
    assert a.cli("start", "1h").returncode == 0
    assert lid_value(env, "AC") == "0"

    second = subprocess.run(
        [*COMMAND, "--no-tray"], env=env, capture_output=True, text=True, timeout=30
    )
    assert "already running" in second.stderr
    assert lid_value(env, "AC") == "0"
    assert (Path(env["XDG_STATE_HOME"]) / "wakeful" / "lid-restore.json").exists()


def test_lid_restored_by_the_watchdog_after_kill_9(stub, app, env):
    set_lid(env, "AC", "1")
    a = app("--no-tray")
    assert call_app(stub, "SetIgnoreLid", "b", True)["ok"]
    assert a.cli("start").returncode == 0
    assert lid_value(env, "AC") == "0"

    a.proc.kill()
    a.proc.wait()
    journal = Path(env["XDG_STATE_HOME"]) / "wakeful" / "lid-restore.json"
    # The journal is removed last, once every value is back.
    wait_for(lambda: not journal.exists(), what="the watchdog to restore the lid")
    assert lid_value(env, "AC") == "1"
    assert lid_value(env, "Battery") == "unset"
    assert stub.dump()["refreshes"] == 2


def test_lid_restored_at_startup_from_a_leftover_journal(stub, env):
    journal = Path(env["XDG_STATE_HOME"]) / "wakeful" / "lid-restore.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"version": 1, "originals": {"AC": "1", "Battery": None, "LowBattery": "2"}})
    )
    for profile in ("AC", "Battery", "LowBattery"):
        set_lid(env, profile, "0")

    a = App(env, "--no-tray")
    try:
        # The restore runs once the bus name is claimed; the journal goes last.
        wait_for(lambda: not journal.exists(), what="the leftover journal to be restored")
        assert [lid_value(env, p) for p in ("AC", "Battery", "LowBattery")] == ["1", "unset", "2"]
    finally:
        a.stop()


# -- following Claude Code --------------------------------------------------


@pytest.fixture
def claude_session(env):
    """A live process standing in for Claude Code, and a writer for its session file."""
    proc = subprocess.Popen(["sleep", "300"])
    sessions = Path(env["CLAUDE_CONFIG_DIR"]) / "sessions"
    sessions.mkdir(parents=True)
    stat = Path(f"/proc/{proc.pid}/stat").read_text()
    started = stat[stat.rfind(")") + 2 :].split()[19]

    def write(status: str) -> None:
        data = {"pid": proc.pid, "procStart": started, "status": status, "name": "fix the tests"}
        tmp = sessions / f"{proc.pid}.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(sessions / f"{proc.pid}.json")

    yield proc, write
    proc.kill()
    proc.wait()


def reasons(stub) -> list[str]:
    return sorted(h["why"] for h in stub.held())


def test_claude_option_needs_the_config_directory(stub, app):
    app("--no-tray")
    result = call_app(stub, "SetWatchClaude", "b", True)
    assert not result["ok"]
    assert "Claude Code was not found" in result["error"]


def test_working_claude_session_keeps_awake_until_idle_or_gone(stub, app, claude_session):
    proc, write = claude_session
    write("shell")
    a = app("--no-tray")
    assert stub.held() == []  # the option is off by default

    status = call_app(stub, "SetWatchClaude", "b", True)["status"]
    assert [s["name"] for s in status["claude_sessions"]] == ["fix the tests"]
    [held] = stub.held()
    assert (held["kind"], held["why"]) == ("pm", "Claude is working: fix the tests")
    out = a.cli("status").stdout
    assert "Keeping awake while Claude is working." in out
    assert "fix the tests (running a shell)" in out

    write("idle")
    wait_for(lambda: stub.held() == [], what="the idle session to be let go")
    assert "Not keeping awake" in a.cli("status").stdout

    write("busy")
    wait_for(lambda: stub.held(), what="the busy session to be followed")
    proc.kill()
    proc.wait()
    wait_for(lambda: stub.held() == [], what="the dead session to be let go")


def test_block_ending_hands_over_to_claude(stub, app, claude_session):
    _, write = claude_session
    write("busy")
    a = app("--no-tray")
    assert call_app(stub, "SetWatchClaude", "b", True)["ok"]
    assert a.cli("start", "--screen", "1h").returncode == 0
    wait_for(
        lambda: reasons(stub) == ["Keeping awake: 1 h left"] * 2,
        what="Claude's inhibition to be retired in favour of the block's",
    )

    assert a.cli("stop").returncode == 0
    # Claude's inhibition is taken before the block's is retired.
    assert "Claude is working: fix the tests" in reasons(stub)
    wait_for(
        lambda: reasons(stub) == ["Claude is working: fix the tests"],
        what="the block's inhibitions to be retired",
    )


def test_tray_follows_claude_from_its_saved_setting(stub, app, env, claude_session):
    _, write = claude_session
    write("busy")
    conf = Path(env["XDG_CONFIG_HOME"]) / "wakeful" / "wakeful.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("[General]\nwatch_claude=true\n")
    a = app()
    wait_for(lambda: stub.held(), what="the tray to follow the session")
    assert "fix the tests (working)" in a.cli("status").stdout
