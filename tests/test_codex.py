import json
import os

from wakeful.codex import CodexWatcher, flock_holders, last_turn_event

THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def event(kind):
    return {"timestamp": "2026-11-14T10:00:00Z", "type": "event_msg", "payload": {"type": kind}}


def home_with(tmp_path, events, locked=True, thread=THREAD):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "11" / "14"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": thread, "cwd": "/work/wakeful"}}
    lines = [meta, *[event(e) if isinstance(e, str) else e for e in events]]
    (day / f"rollout-2026-11-14T10-00-00-{thread}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )
    locks_dir = home / "thread-writer-locks"
    locks_dir.mkdir(exist_ok=True)
    lock = locks_dir / f"{thread}.lock"
    lock.touch()
    st = lock.stat()
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc_locks = tmp_path / "locks"
    text = proc_locks.read_text() if proc_locks.exists() else ""
    if locked:
        text += f"1: FLOCK  ADVISORY  WRITE 4242 {dev} 0 EOF\n"
    proc_locks.write_text(text)
    return CodexWatcher(home, locks=proc_locks)


def test_a_locked_thread_whose_last_turn_started_is_working(tmp_path):
    w = home_with(tmp_path, ["task_complete", "task_started", {"type": "response_item"}])
    [s] = w.working()
    assert (s.agent, s.pid, s.name, s.cwd) == ("Codex", 4242, "wakeful", "/work/wakeful")


def test_finished_or_aborted_turns_are_not_working(tmp_path):
    assert home_with(tmp_path / "a", ["task_started", "task_complete"]).working() == ()
    assert home_with(tmp_path / "b", ["task_started", "turn_aborted"]).working() == ()


def test_older_event_names_are_read(tmp_path):
    assert len(home_with(tmp_path / "a", ["turn_started"]).working()) == 1
    assert home_with(tmp_path / "b", ["turn_started", "turn_complete"]).working() == ()


def test_a_thread_whose_process_died_is_not_working(tmp_path):
    assert home_with(tmp_path, ["task_started"], locked=False).working() == ()


def test_the_last_turn_event_is_found_behind_large_output(tmp_path):
    big = {"type": "response_item", "payload": {"text": "x" * 200_000}}
    w = home_with(tmp_path, ["task_started", big, big])
    assert len(w.working()) == 1


def test_last_turn_event_ignores_lines_that_only_mention_one(tmp_path):
    path = tmp_path / "r.jsonl"
    lines = [event("task_started"), {"type": "response_item", "payload": {"text": "task_complete"}}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + '{"half')
    assert last_turn_event(path) == "task_started"


def test_flock_holders_skips_waiters_and_other_kinds():
    text = (
        "1: FLOCK  ADVISORY  WRITE 4242 fd:01:131 0 EOF\n"
        "1: -> FLOCK  ADVISORY  WRITE 4343 fd:01:131 0 EOF\n"
        "2: POSIX  ADVISORY  WRITE 99 fd:01:200 0 EOF\n"
    )
    assert flock_holders(text) == {(0xFD, 1, 131): 4242}


def test_available_needs_more_than_an_empty_directory(tmp_path):
    w = CodexWatcher(tmp_path)
    assert not w.available()
    (tmp_path / "config.toml").touch()
    assert w.available()
