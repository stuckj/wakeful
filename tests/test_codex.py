import json
import os
import time

from wakeful.codex import CodexWatcher, flock_holders, last_turn_event

THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def event(kind):
    return {"timestamp": "2026-11-14T10:00:00Z", "type": "event_msg", "payload": {"type": kind}}


def write_rollout(day, name, events, cwd="/work/wakeful"):
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": THREAD, "cwd": cwd}}
    lines = [meta, *[event(e) if isinstance(e, str) else e for e in events]]
    (day / name).write_text("".join(json.dumps(line) + "\n" for line in lines))
    return day / name


def home_with(tmp_path, events, locked=True, thread=THREAD, device=None):
    """A Codex home with one thread, and a fake /proc whose locks file names its holder.

    device overrides the major:minor /proc/locks reports, as btrfs does.
    """
    home = tmp_path / "codex"
    write_rollout(
        home / "sessions" / "2026" / "11" / "14",
        f"rollout-2026-11-14T10-00-00-{thread}.jsonl",
        events,
    )
    locks_dir = home / "thread-writer-locks"
    locks_dir.mkdir(exist_ok=True)
    lock = locks_dir / f"{thread}.lock"
    lock.touch()
    st = lock.stat()
    dev = device or f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
    proc = tmp_path / "proc"
    (proc / "4242" / "fd").mkdir(parents=True, exist_ok=True)
    os.symlink(lock, proc / "4242" / "fd" / "7")
    locks = proc / "locks"
    text = locks.read_text() if locks.exists() else ""
    if locked:
        text += f"1: FLOCK  ADVISORY  WRITE 4242 {dev}:{st.st_ino} 0 EOF\n"
    locks.write_text(text)
    return CodexWatcher(home, proc=proc)


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


def test_lock_files_that_are_not_threads_are_skipped(tmp_path):
    w = home_with(tmp_path, ["task_started"])
    (w.home / "thread-writer-locks" / ".coordination.lock").touch()
    looked_up, find = [], w._rollout
    w._rollout = lambda thread: looked_up.append(thread) or find(thread)
    assert len(w.working()) == 1
    assert looked_up == [THREAD]


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
    assert flock_holders(text) == {131: [(0xFD, 1, 4242)]}


def test_a_holder_reported_on_another_device_is_confirmed_by_its_open_file(tmp_path):
    w = home_with(tmp_path, ["task_started"], device="00:3b")
    [s] = w.working()
    assert s.pid == 4242


def test_an_inode_match_without_the_file_open_does_not_count(tmp_path):
    w = home_with(tmp_path, ["task_started"], device="00:3b")
    (tmp_path / "proc" / "4242" / "fd" / "7").unlink()
    assert w.working() == ()


def test_an_edited_prompt_moves_the_thread_to_a_new_rollout(tmp_path):
    w = home_with(tmp_path, ["task_started", "task_complete"])
    assert w.working() == ()
    today = w.home / "sessions" / time.strftime("%Y/%m/%d")
    newer = write_rollout(
        today, f"rollout-2026-11-14T11-00-00-{THREAD}_0199b.jsonl", ["task_started"]
    )
    os.utime(newer, (time.time() + 5, time.time() + 5))
    assert len(w.working()) == 1


def test_available_needs_more_than_an_empty_directory(tmp_path):
    w = CodexWatcher(tmp_path)
    assert not w.available()
    (tmp_path / "config.toml").touch()
    assert w.available()
