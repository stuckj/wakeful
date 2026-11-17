import json
import os
import sqlite3

import pytest

from wakeful.opencode import REQUERY_AFTER, SILENT_FOR, OpencodeWatcher

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
    conn.execute("CREATE TABLE part (id TEXT, session_id TEXT, time_updated INT)")
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
    tmp_path,
    cwd="/work/app",
    started_ago=60,
    exe="/usr/lib/opencode-ai/bin/opencode.exe",
    args=(),
):
    """A /proc with one process started started_ago seconds before NOW."""
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "stat").write_text(f"cpu 1 2 3\nbtime {BOOT}\n")
    entry = proc / "4242"
    entry.mkdir()
    os.symlink(exe, entry / "exe")
    os.symlink(cwd, entry / "cwd")
    (entry / "cmdline").write_bytes(b"\0".join([b"opencode", *(a.encode() for a in args)]) + b"\0")
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


def test_the_nixpkgs_wrapped_binary_counts(watcher):
    exe = "/nix/store/abc-opencode-1.19.5/bin/.opencode-wrapped"
    assert len(watcher([("assistant", False, 5)], exe=exe).working()) == 1


def test_an_upgraded_binary_still_counts(watcher):
    exe = "/usr/lib/opencode-ai/bin/opencode.exe (deleted)"
    assert len(watcher([("assistant", False, 5)], exe=exe).working()) == 1


def test_a_step_left_by_a_crash_before_this_process_started_is_ignored(watcher):
    w = watcher([("assistant", False, 3600)], started_ago=60)
    assert w.working() == ()


def test_a_step_left_by_a_crash_while_an_older_opencode_runs_there_expires(watcher):
    w = watcher([("assistant", False, SILENT_FOR + 60)], started_ago=SILENT_FOR + 3600)
    assert w.working() == ()


def test_a_long_step_counts_while_its_session_is_being_written(tmp_path, watcher):
    w = watcher([("assistant", False, SILENT_FOR + 60)], started_ago=SILENT_FOR + 3600)
    conn = sqlite3.connect(tmp_path / "data" / "opencode.db")
    conn.execute("INSERT INTO part VALUES ('p1', 's1', ?)", (int((NOW - 30) * 1000),))
    conn.commit()
    conn.close()
    assert len(w.working()) == 1


def test_opencode_in_a_parent_directory_does_not_count(watcher):
    assert watcher([("assistant", False, 5)], cwd="/work").working() == ()


def test_opencode_in_a_subdirectory_counts(watcher):
    assert len(watcher([("assistant", False, 5)], cwd="/work/app/src").working()) == 1


def test_the_database_is_not_opened_while_opencode_is_not_running(tmp_path):
    home = tmp_path / "data"
    v1_db(home, [("assistant", False, 5)])
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"btime {BOOT}\n")
    w = OpencodeWatcher(home, proc, now=Clock())
    w._busy_sessions = lambda db, since: pytest.fail("queried with no opencode running")
    assert w.working() == ()


def test_the_query_is_repeated_only_when_the_database_changes(tmp_path, watcher):
    clock = Clock()
    v1_db(tmp_path / "data", [("assistant", True, 5)])
    w = OpencodeWatcher(tmp_path / "data", fake_proc(tmp_path), now=clock)
    queries = []
    query = w._busy_sessions
    w._busy_sessions = lambda db, since: queries.append(since) or query(db, since)
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


def add_v2_session(db, title, busy_ago, idle=False):
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_v2 (id TEXT, title TEXT, directory TEXT, "
        "time_busy INT, time_idle INT)"
    )
    busy = int((NOW - busy_ago) * 1000)
    conn.execute(
        "INSERT INTO session_v2 VALUES (?, ?, '/work/app', ?, ?)",
        (title, title, busy, busy + 1 if idle else None),
    )
    conn.commit()
    conn.close()


def test_a_migrated_database_is_read_for_both_versions(tmp_path):
    """2.x keeps 1.x's tables beside its own; either version may be running."""
    home = tmp_path / "data"
    v1_db(home, [("assistant", False, 5)])
    add_v2_session(home / "opencode.db", "v2 run", 5)
    w = OpencodeWatcher(home, fake_proc(tmp_path), now=Clock())
    assert sorted(s.name for s in w.working()) == ["fix the tests", "v2 run"]


def test_the_2x_background_service_runs_sessions_of_any_directory(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    add_v2_session(home / "opencode.db", "run in the background", 5)
    proc = fake_proc(tmp_path, cwd="/home/user", args=("serve", "--service"))
    w = OpencodeWatcher(home, proc, now=Clock())
    assert [s.name for s in w.working()] == ["run in the background"]


def test_the_service_does_not_revive_a_run_older_than_itself(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    add_v2_session(home / "opencode.db", "left by a crash", 3600)
    proc = fake_proc(tmp_path, cwd="/home/user", started_ago=60, args=("serve", "--service"))
    assert OpencodeWatcher(home, proc, now=Clock()).working() == ()


def test_another_opencode_outside_the_directory_still_does_not_count(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    add_v2_session(home / "opencode.db", "elsewhere", 5)
    proc = fake_proc(tmp_path, cwd="/home/user", args=("serve", "--port", "4096"))
    assert OpencodeWatcher(home, proc, now=Clock()).working() == ()


def test_an_unreadable_database_does_not_hide_the_others(tmp_path):
    home = tmp_path / "data"
    v1_db(home, [("assistant", False, 5)])
    (home / "opencode-broken.db").write_bytes(b"not a database" * 100)
    w = OpencodeWatcher(home, fake_proc(tmp_path), now=Clock())
    assert [s.name for s in w.working()] == ["fix the tests"]


def test_a_channel_database_is_read(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    add_v2_session(home / "opencode-beta.db", "beta run", 5)
    w = OpencodeWatcher(home, fake_proc(tmp_path), now=Clock())
    assert w.available()
    assert [s.name for s in w.working()] == ["beta run"]


def test_available_follows_the_database(tmp_path):
    w = OpencodeWatcher(tmp_path)
    assert not w.available()
    (tmp_path / "opencode.db").touch()
    assert w.available()


def test_a_failed_read_is_retried_on_the_next_tick(tmp_path, watcher):
    w = watcher([("assistant", False, 5)])
    query, failures = w._busy_sessions, [1]

    def flaky(db, since):
        if failures:
            failures.pop()
            raise sqlite3.OperationalError("database is locked")
        return query(db, since)

    w._busy_sessions = flaky
    assert w.working() == ()
    assert len(w.working()) == 1
