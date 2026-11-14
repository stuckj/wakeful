import json
import os
import sqlite3

import pytest

from wakeful.opencode import STALE_AFTER, OpencodeWatcher

NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def v1_db(home, messages):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "opencode.db")
    conn.execute("CREATE TABLE session (id TEXT, title TEXT, directory TEXT)")
    conn.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, "
        "time_updated INT, data TEXT)"
    )
    conn.execute("CREATE TABLE credential (id TEXT, secret TEXT)")
    conn.execute("INSERT INTO session VALUES ('s1', 'fix the tests', '/work/app')")
    for i, (role, completed, age) in enumerate(messages):
        t = int((NOW - age) * 1000)
        data = {"role": role, "time": {"created": t, **({"completed": t} if completed else {})}}
        conn.execute(
            "INSERT INTO message VALUES (?, 's1', ?, ?, ?)", (f"m{i}", t, t, json.dumps(data))
        )
    conn.commit()
    conn.close()


def proc_with(tmp_path, cwd, exe="/usr/lib/opencode-ai/bin/opencode.exe"):
    proc = tmp_path / "proc"
    entry = proc / "4242"
    entry.mkdir(parents=True)
    os.symlink(exe, entry / "exe")
    os.symlink(cwd, entry / "cwd")
    (proc / "self").mkdir()
    return proc


@pytest.fixture
def watcher(tmp_path):
    def make(messages, cwd="/work/app", exe="/usr/lib/opencode-ai/bin/opencode.exe"):
        v1_db(tmp_path / "data", messages)
        return OpencodeWatcher(tmp_path / "data", proc_with(tmp_path, cwd, exe), now=Clock())

    return make


def test_an_unfinished_assistant_step_with_opencode_running_there_is_working(watcher):
    w = watcher([("user", True, 10), ("assistant", False, 5)])
    [s] = w.working()
    assert (s.agent, s.pid, s.name, s.cwd) == ("opencode", 4242, "fix the tests", "/work/app")


def test_finished_steps_are_not_working(watcher):
    assert watcher([("user", True, 10), ("assistant", True, 5)]).working() == ()


def test_needs_an_opencode_process_in_that_directory(watcher):
    assert watcher([("assistant", False, 5)], cwd="/elsewhere").working() == ()


def test_another_program_in_that_directory_does_not_count(watcher):
    assert watcher([("assistant", False, 5)], exe="/usr/bin/bash").working() == ()


def test_an_unfinished_step_left_by_a_crash_long_ago_is_ignored(watcher):
    assert watcher([("assistant", False, STALE_AFTER + 60)]).working() == ()


def test_the_query_is_repeated_only_when_the_database_changes(tmp_path, watcher):
    w = watcher([("assistant", True, 5)])
    assert w.working() == ()
    db = tmp_path / "data" / "opencode.db"
    conn = sqlite3.connect(db)
    data = json.dumps({"role": "assistant", "time": {"created": 1}})
    t = int(NOW * 1000)
    conn.execute("INSERT INTO message VALUES ('m9', 's1', ?, ?, ?)", (t, t, data))
    conn.commit()
    conn.close()
    os.utime(db, ns=(1, 1))  # a new stamp even if the file kept its size
    assert len(w.working()) == 1


def test_version_2_busy_and_idle_times(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    conn = sqlite3.connect(home / "opencode.db")
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT, title TEXT, directory TEXT, "
        "time_busy INT, time_idle INT)"
    )
    busy_ms = int((NOW - 5) * 1000)
    conn.execute("INSERT INTO session_v2 VALUES ('a', 'busy', '/work/app', ?, NULL)", (busy_ms,))
    conn.execute(
        "INSERT INTO session_v2 VALUES ('b', 'done', '/work/app', ?, ?)", (busy_ms, busy_ms + 1)
    )
    conn.commit()
    conn.close()
    w = OpencodeWatcher(home, proc_with(tmp_path, "/work/app"), now=Clock())
    assert [s.name for s in w.working()] == ["busy"]


def test_available_follows_the_database(tmp_path):
    w = OpencodeWatcher(tmp_path)
    assert not w.available()
    assert w.working() == ()
    (tmp_path / "opencode.db").touch()
    assert w.available()
