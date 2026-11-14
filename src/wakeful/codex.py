"""Which Codex CLI threads on this machine are working right now.

Qt-free. Codex (github.com/openai/codex) keeps no status file, but two things
it writes say the same. Neither is a documented interface:

  $CODEX_HOME/thread-writer-locks/<thread>.lock
      flock-ed by the process writing that thread for as long as it is open.
      The kernel drops the lock when the process dies; the file stays.
  $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl
      One JSON event per line. A turn writes {"type": "event_msg", "payload":
      {"type": "task_started"}} when it begins and "task_complete" or
      "turn_aborted" when it ends ("turn_started"/"turn_complete" in older
      versions). The first line is "session_meta", whose payload has the cwd.

A thread is working while its lock is held and the last of those turn events
is a start. Waiting for approval happens inside a turn, so it counts as working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .agents import AgentSession

STARTED = {"task_started", "turn_started"}
ENDED = {"task_complete", "turn_complete", "turn_aborted"}
TAIL_CHUNK = 64 * 1024
TAIL_MAX = 4 * 1024 * 1024
"""Read at most this much from the end of a rollout looking for a turn event."""


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def flock_holders(text: str) -> dict[tuple[int, int, int], int]:
    """Map (major, minor, inode) to the holding pid, from /proc/locks."""
    holders: dict[tuple[int, int, int], int] = {}
    for line in text.splitlines():
        fields = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1234 fd:01:5678 0 EOF"; a waiter's line
        # has "->" where FLOCK is.
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            holders[(int(major, 16), int(minor, 16), int(inode))] = int(fields[4])
        except ValueError:
            continue
    return holders


def last_turn_event(path: Path) -> str | None:
    """The type of the last turn start or end event in a rollout, read from its end."""
    with path.open("rb") as f:
        end = f.seek(0, os.SEEK_END)
        pos, carry = end, b""
        while pos > 0 and end - pos < TAIL_MAX:
            step = min(TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + carry).split(b"\n")
            carry = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                found = _turn_event(line)
                if found:
                    return found
        return None


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

    def __init__(self, home: Path | None = None, locks: Path = Path("/proc/locks")) -> None:
        self.home = home or codex_home()
        self._locks = locks
        self._rollouts: dict[str, Path] = {}
        self._cwds: dict[Path, str] = {}

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
        holders = flock_holders(self._locks.read_text())
        found = []
        for lock in sorted(lock_files):
            if lock.name.startswith("."):
                continue  # .coordination.lock and the like are not threads
            try:
                st = lock.stat()
            except OSError:
                continue
            pid = holders.get((os.major(st.st_dev), os.minor(st.st_dev), st.st_ino))
            if pid is None:
                continue
            rollout = self._rollout(lock.stem)
            if rollout is None:
                continue
            try:
                if last_turn_event(rollout) not in STARTED:
                    continue
            except OSError:
                continue
            cwd = self._cwd(rollout)
            name = Path(cwd).name if cwd else lock.stem[:8]
            found.append(AgentSession(self.label, pid, name, "working", cwd))
        return tuple(found)

    def _rollout(self, thread: str) -> Path | None:
        cached = self._rollouts.get(thread)
        if cached is not None and cached.exists():
            return cached
        matches = sorted((self.home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
        if not matches:
            return None
        self._rollouts[thread] = matches[-1]
        return matches[-1]

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
