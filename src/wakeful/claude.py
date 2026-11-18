"""Which Claude Code sessions on this machine are working right now.

Qt-free. Claude Code keeps a file per running process in
$CLAUDE_CONFIG_DIR/sessions/<pid>.json (default ~/.claude/sessions). This is
not a documented interface; the fields read here are:

  status      "busy" during a turn, including while an in-process subagent runs;
              "shell" once the turn has ended while a background shell is still
              running; "idle" at the prompt; "waiting" when blocked on the user.
  name        the session's title, as its session list shows it.
  cwd         the session's working directory.
  procStart   field 22 of /proc/<pid>/stat for the process that wrote the file.
  pidDomain   "linux:<machine-id>:pid:[<pid namespace inode>]".

A file outlives a process that crashed, so a session counts only while its PID
is alive with the same start time, in this machine's PID namespace.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from .agents import AgentSession, proc_start

log = logging.getLogger(__name__)

WORKING = {"busy": "working", "shell": "running a shell"}


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def pid_domain() -> str | None:
    try:
        machine = Path("/etc/machine-id").read_text().strip()
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    return f"linux:{machine}:{namespace}"


class ClaudeWatcher:
    """Reads the sessions directory and reports the sessions that are working."""

    key = "claude"
    label = "Claude Code"

    def __init__(
        self,
        sessions_dir: Path | None = None,
        start_time: Callable[[int], int | None] = proc_start,
        domain: str | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir or config_dir() / "sessions"
        self._start_time = start_time
        self._domain = domain if domain is not None else pid_domain()

    def available(self) -> bool:
        """Claude Code's configuration directory exists."""
        return self.sessions_dir.parent.is_dir()

    def working(self) -> tuple[AgentSession, ...]:
        try:
            files = sorted(self.sessions_dir.glob("*.json"))
        except OSError:
            return ()
        found = []
        for path in files:
            session = self._read(path)
            if session is not None:
                found.append(session)
        return tuple(found)

    def _read(self, path: Path) -> AgentSession | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            # Claude Code may be rewriting it; the next poll reads it again.
            return None
        if not isinstance(data, dict) or str(data.get("status")) not in WORKING:
            return None
        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return None
        domain = data.get("pidDomain")
        if domain is not None and self._domain is not None and domain != self._domain:
            return None
        started = self._start_time(pid)
        if started is None:
            return None
        expected = data.get("procStart")
        if expected is not None and str(expected) != str(started):
            return None
        name = data.get("name") or str(data.get("sessionId", ""))[:8] or str(pid)
        doing = WORKING[str(data["status"])]
        return AgentSession(self.label, pid, str(name), doing, str(data.get("cwd", "")))
