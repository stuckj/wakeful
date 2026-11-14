import json
import os
import sqlite3

import pytest

from wakeful.opencode import REQUERY_AFTER, OpencodeWatcher

BOOT = 1_799_990_000
NOW = 1_800_000_000.0
TICKS = os.sysconf("SC_CLK_TCK")


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def v1_db(home, steps):
    """steps: (role, completed, seconds before NOW it began)."""
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "opencode.db")
    conn.execute("CREATE TABLE session (id TEXT, title TEXT, directory TEXT)")
    conn.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, "
        "time_updated INT, data TEXT)"
    )
    conn.execute("CREATE TABLE credential (id TEXT, secret TEXT)")
    conn.execute("INSERT INTO session VALUES ('s1', 'fix the tests', '/work/app')")
    for i, step in enumerate(steps):
        add_step(conn, f"m{i}", *step)
    conn.commit()
    conn.close()


def add_step(conn, id_, role, completed, age):
    t = int((NOW - age) * 1000)
    data = {"role": role, "time": {"created": t, **({"completed": t} if completed else {})}}
    conn.execute("INSERT INTO message VALUES (?, 's1', ?, ?, ?)", (id_, t, t, json.dumps(data)))


def fake_proc(
    tmp_path, cwd="/work/app", started_ago=60, exe="/usr/lib/opencode-ai/bin/opencode.exe"
):
    """A /proc with one process started started_ago seconds before NOW."""
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "stat").write_text(f"cpu 1 2 3\nbtime {BOOT}\n")
    entry = proc / "4242"
    entry.mkdir()
    os.symlink(exe, entry / "exe")
    os.symlink(cwd, entry / "cwd")
    start_ticks = int((NOW - started_ago - BOOT) * TICKS)
    fields = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 5
    (entry / "stat").write_text(f"4242 (opencode.exe) {' '.join(fields)}\n")
    return proc


@pytest.fixture
def watcher(tmp_path):
    def make(steps, **proc):
        v1_db(tmp_path / "data", steps)
        return OpencodeWatcher(tmp_path / "data", fake_proc(tmp_path, **proc), now=Clock())

    return make


def test_an_unfinished_step_of_a_running_opencode_there_is_working(watcher):
    w = watcher([("user", True, 10), ("assistant", False, 5)])
    [s] = w.working()
    assert (s.agent, s.pid, s.name, s.cwd) == ("opencode", 4242, "fix the tests", "/work/app")


def test_finished_steps_are_not_working(watcher):
    assert watcher([("user", True, 10), ("assistant", True, 5)]).working() == ()


def test_needs_an_opencode_process_in_that_directory(watcher):
    assert watcher([("assistant", False, 5)], cwd="/elsewhere").working() == ()


def test_another_program_in_that_directory_does_not_count(watcher):
    assert watcher([("assistant", False, 5)], exe="/usr/bin/bash").working() == ()


def test_an_upgraded_binary_still_counts(watcher):
    exe = "/usr/lib/opencode-ai/bin/opencode.exe (deleted)"
    assert len(watcher([("assistant", False, 5)], exe=exe).working()) == 1


def test_a_step_left_by_a_crash_before_this_process_started_is_ignored(watcher):
    w = watcher([("assistant", False, 3600)], started_ago=60)
    assert w.working() == ()


def test_the_database_is_not_opened_while_opencode_is_not_running(tmp_path):
    home = tmp_path / "data"
    v1_db(home, [("assistant", False, 5)])
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"btime {BOOT}\n")
    w = OpencodeWatcher(home, proc, now=Clock())
    w._busy_sessions = lambda since: pytest.fail("queried with no opencode running")
    assert w.working() == ()


def test_the_query_is_repeated_only_when_the_database_changes(tmp_path, watcher):
    clock = Clock()
    v1_db(tmp_path / "data", [("assistant", True, 5)])
    w = OpencodeWatcher(tmp_path / "data", fake_proc(tmp_path), now=clock)
    queries = []
    query = w._busy_sessions
    w._busy_sessions = lambda since: queries.append(since) or query(since)
    assert w.working() == ()
    clock.t += REQUERY_AFTER - 1
    assert w.working() == ()
    assert len(queries) == 1  # unchanged and within the minute: not queried again

    db = tmp_path / "data" / "opencode.db"
    conn = sqlite3.connect(db)
    add_step(conn, "m9", "assistant", False, 1)
    conn.commit()
    conn.close()
    os.utime(db, ns=(1, 1))  # a new stamp even if the file kept its size
    assert len(w.working()) == 1
    assert len(queries) == 2


def test_version_2_busy_and_idle_times(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    conn = sqlite3.connect(home / "opencode.db")
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT, title TEXT, directory TEXT, "
        "time_busy INT, time_idle INT)"
    )
    busy_ms = int((NOW - 5) * 1000)
    stale_ms = int((NOW - 3600) * 1000)
    rows = [
        ("a", "busy", busy_ms, None),
        ("b", "done", busy_ms, busy_ms + 1),
        ("c", "crashed", stale_ms, None),
    ]
    for id_, title, busy, idle in rows:
        conn.execute(
            "INSERT INTO session_v2 VALUES (?, ?, '/work/app', ?, ?)", (id_, title, busy, idle)
        )
    conn.commit()
    conn.close()
    w = OpencodeWatcher(home, fake_proc(tmp_path), now=Clock())
    assert [s.name for s in w.working()] == ["busy"]


def test_available_follows_the_database(tmp_path):
    w = OpencodeWatcher(tmp_path)
    assert not w.available()
    (tmp_path / "opencode.db").touch()
    assert w.available()
