import json
import os

from wakeful.claude import SessionWatcher, proc_start

DOMAIN = "linux:abc:pid:[1]"


def write(d, pid, **fields):
    data = {
        "pid": pid,
        "sessionId": "3f557f51-f2df-44fc-845a-be1d5613e4dc",
        "cwd": "/work",
        "procStart": "500",
        "pidDomain": DOMAIN,
        "name": f"session {pid}",
        "status": "busy",
    }
    data.update(fields)
    data = {k: v for k, v in data.items() if v is not None}
    (d / f"{pid}.json").write_text(json.dumps(data))


def watcher(d, alive=None):
    alive = {100: 500, 200: 500, 300: 500} if alive is None else alive
    return SessionWatcher(d, start_time=alive.get, domain=DOMAIN)


def names(w):
    return [s.name for s in w.working()]


def test_busy_and_shell_count_idle_and_waiting_do_not(tmp_path):
    write(tmp_path, 100, status="busy")
    write(tmp_path, 200, status="shell")
    write(tmp_path, 300, status="idle")
    write(tmp_path, 400, status="waiting")
    w = watcher(tmp_path, {100: 500, 200: 500, 300: 500, 400: 500})
    assert names(w) == ["session 100", "session 200"]
    assert [s.doing for s in w.working()] == ["working", "running a shell"]


def test_file_left_by_a_dead_process_is_ignored(tmp_path):
    write(tmp_path, 100)
    assert names(watcher(tmp_path, {})) == []


def test_reused_pid_is_ignored(tmp_path):
    write(tmp_path, 100, procStart="499")
    assert names(watcher(tmp_path)) == []


def test_session_in_another_pid_namespace_is_ignored(tmp_path):
    write(tmp_path, 100, pidDomain="linux:abc:pid:[2]")
    assert names(watcher(tmp_path)) == []


def test_older_files_without_domain_or_start_are_accepted_while_alive(tmp_path):
    write(tmp_path, 100, pidDomain=None, procStart=None)
    assert names(watcher(tmp_path)) == ["session 100"]


def test_unnamed_session_falls_back_to_its_id(tmp_path):
    write(tmp_path, 100, name=None)
    assert names(watcher(tmp_path)) == ["3f557f51"]


def test_unreadable_files_and_missing_directory_are_skipped(tmp_path):
    (tmp_path / "100.json").write_text("{half")
    (tmp_path / "200.json").write_text("[]")
    (tmp_path / "300.json").write_text(json.dumps({"pid": "300", "status": "busy"}))
    assert names(watcher(tmp_path)) == []
    assert names(watcher(tmp_path / "missing")) == []


def test_proc_start_reads_this_process():
    assert isinstance(proc_start(os.getpid()), int)
    assert proc_start(2**22 + 1) is None
