"""Which Codex CLI threads on this machine are working right now.

Qt-free. Codex (github.com/openai/codex) keeps no status file, but two things
it writes say the same. Neither is a documented interface:

  $CODEX_HOME/thread-writer-locks/<thread>.lock
      flock-ed by the process writing that thread for as long as it is open.
      The kernel drops the lock when the process dies; the file stays.
  $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread>[_<id>].jsonl
      One JSON event per line; editing an earlier prompt starts a new file.
      A turn writes {"type": "event_msg", "payload": {"type": "task_started"}}
      when it begins and "task_complete" or
      "turn_aborted" when it ends ("turn_started"/"turn_complete" in older
      versions). The first line is "session_meta", whose payload has the cwd.

A thread is working while its lock is held and the last of those turn events
is a start. Waiting for approval happens inside a turn, so it counts as working.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .agents import AgentSession

STARTED = {"task_started", "turn_started"}
ENDED = {"task_complete", "turn_complete", "turn_aborted"}
TAIL_CHUNK = 64 * 1024


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def flock_holders(text: str) -> dict[int, list[tuple[int, int, int]]]:
    """Map each inode to the (major, minor, pid) of its flock holders, from /proc/locks."""
    holders: dict[int, list[tuple[int, int, int]]] = {}
    for line in text.splitlines():
        fields = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1234 fd:01:5678 0 EOF"; a waiter's line
        # has "->" where FLOCK is.
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            entry = (int(major, 16), int(minor, 16), int(fields[4]))
        except ValueError:
            continue
        holders.setdefault(int(inode), []).append(entry)
    return holders


def has_open(proc: Path, pid: int, st: os.stat_result) -> bool:
    """pid has the file st describes open, by the identity of its /proc fds."""
    try:
        for fd in (proc / str(pid) / "fd").iterdir():
            try:
                target = fd.stat()
            except OSError:
                continue
            if (target.st_dev, target.st_ino) == (st.st_dev, st.st_ino):
                return True
    except OSError:
        return False
    return False


def last_turn_event(path: Path, start: int = 0) -> tuple[str | None, int]:
    """The last turn start or end event among the complete lines from byte start on,
    read from the end, and the offset where those complete lines end."""
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        carry, complete = b"", None
        while pos > start:
            step = min(TAIL_CHUNK, pos - start)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + carry
            if complete is None:
                # A line still being written is left for the next read.
                newline = chunk.rfind(b"\n")
                if newline < 0:
                    carry = chunk
                    continue
                complete = pos + newline + 1
                chunk = chunk[: newline + 1]
            lines = chunk.split(b"\n")
            carry = lines.pop(0) if pos > start else b""
            for line in reversed(lines):
                found = _turn_event(line)
                if found:
                    return found, complete
        return None, start if complete is None else complete


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _turn_event(line: bytes) -> str | None:
    if b"task_" not in line and b"turn_" not in line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "event_msg":
        return None
    payload = event.get("payload")
    kind = payload.get("type") if isinstance(payload, dict) else None
    return kind if kind in STARTED or kind in ENDED else None


class CodexWatcher:
    key = "codex"
    label = "Codex"

    def __init__(self, home: Path | None = None, proc: Path = Path("/proc")) -> None:
        self.home = home or codex_home()
        self._proc = proc
        self._rollouts: dict[str, Path] = {}
        self._cwds: dict[Path, str] = {}
        self._confirmed: set[tuple[int, int]] = set()
        """(inode, pid) pairs already seen holding a lock file open."""
        self._scans: dict[Path, tuple[int, int, str | None]] = {}
        """Per rollout: its inode, how far it has been read, and the last turn event."""

    def available(self) -> bool:
        return any((self.home / n).exists() for n in ("sessions", "config.toml", "auth.json"))

    def working(self) -> tuple[AgentSession, ...]:
        lock_dir = self.home / "thread-writer-locks"
        try:
            lock_files = list(lock_dir.glob("*.lock"))
        except OSError:
            return ()
        if not lock_files:
            return ()
        holders = flock_holders((self._proc / "locks").read_text())
        found = []
        for lock in sorted(lock_files):
            if lock.name.startswith("."):
                continue  # .coordination.lock and the like are not threads
            try:
                st = lock.stat()
            except OSError:
                continue
            pid = self._holder(lock, st, holders.get(st.st_ino, []))
            if pid is None:
                continue
            rollout = self._rollout(lock.stem)
            if rollout is None:
                continue
            try:
                if self._turn(rollout) not in STARTED:
                    continue
            except OSError:
                continue
            cwd = self._cwd(rollout)
            name = Path(cwd).name if cwd else lock.stem[:8]
            found.append(AgentSession(self.label, pid, name, "working", cwd))
        return tuple(found)

    def _turn(self, rollout: Path) -> str | None:
        """The last turn event in rollout, reading only what was appended since last time."""
        st = rollout.stat()
        scanned = self._scans.get(rollout)
        if scanned is not None and scanned[0] == st.st_ino and st.st_size >= scanned[1]:
            event, end = last_turn_event(rollout, scanned[1])
            event = event or scanned[2]
        else:
            event, end = last_turn_event(rollout)
        self._scans[rollout] = (st.st_ino, end, event)
        return event

    def _holder(self, lock: Path, st: os.stat_result, entries: list) -> int | None:
        """The pid holding lock, or None. /proc/locks names the filesystem's device,
        which on btrfs is not the subvolume's st_dev, so an inode match is confirmed
        by the pid having the file open."""
        for major, minor, pid in entries:
            if (major, minor) == (os.major(st.st_dev), os.minor(st.st_dev)):
                return pid
        for _major, _minor, pid in entries:
            if (st.st_ino, pid) in self._confirmed or has_open(self._proc, pid, st):
                self._confirmed.add((st.st_ino, pid))
                return pid
        return None

    def _rollout(self, thread: str) -> Path | None:
        """The thread's newest rollout. Editing an earlier prompt starts a new one,
        rollout-<time>-<thread>_<id>.jsonl, in the folder for the day it was made."""
        sessions = self.home / "sessions"
        pattern = f"rollout-*-{thread}*.jsonl"
        cached = self._rollouts.get(thread)
        folders = {
            sessions / time.strftime("%Y/%m/%d", t) for t in (time.localtime(), time.gmtime())
        }
        if cached is not None:
            folders.add(cached.parent)
        matches = [p for folder in folders for p in folder.glob(pattern)]
        if cached is None or not matches:
            matches = list(sessions.glob(f"*/*/*/{pattern}"))
        newest = max(matches, key=_mtime, default=None)
        if newest is not None:
            self._rollouts[thread] = newest
        return newest

    def _cwd(self, rollout: Path) -> str:
        if rollout not in self._cwds:
            cwd = ""
            try:
                with rollout.open("rb") as f:
                    first = json.loads(f.readline(TAIL_CHUNK))
                payload = first.get("payload") if isinstance(first, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
            except (OSError, ValueError):
                pass
            self._cwds[rollout] = cwd
        return self._cwds[rollout]
