"""Which opencode sessions on this machine are working right now.

Qt-free. opencode (opencode.ai) keeps its working state in memory only, but its
SQLite database at $XDG_DATA_HOME/opencode/opencode.db shows the same. Neither
is a documented interface:

  1.x  An assistant message is written when a step starts, with
       data.time.completed unset, and completed when the step and its tools
       finish. A session with such a message is working.
  2.x  session_v2.time_busy is set when a run starts and time_idle when it
       ends; the session is working while time_busy is the later.

The database is opened read-only, and only the session and message tables are
read; the same file holds credentials. It records no process, so a session
counts only while an opencode process runs in its directory, and only if the
unfinished step is recent: a crash leaves one behind for good.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .agents import AgentSession

STALE_AFTER = 3 * 3600
"""An unfinished step this old (seconds) is taken to be left by a crash."""
REQUERY_AFTER = 60
BINARIES = {"opencode", "opencode.exe"}

WORKING_V1 = """
    SELECT s.id, s.title, s.directory
    FROM message m JOIN session s ON s.id = m.session_id
    WHERE m.time_updated >= ?
      AND json_extract(m.data, '$.role') = 'assistant'
      AND json_extract(m.data, '$.time.completed') IS NULL
    GROUP BY s.id
"""
WORKING_V2 = """
    SELECT id, title, directory, time_busy FROM session_v2
    WHERE time_busy IS NOT NULL AND (time_idle IS NULL OR time_idle < time_busy)
"""


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode"


def opencode_cwds(proc: Path = Path("/proc")) -> dict[str, int]:
    """Working directory to pid, for every opencode process this user can see."""
    found: dict[str, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if Path(os.readlink(entry / "exe")).name not in BINARIES:
                continue
            found[os.readlink(entry / "cwd")] = int(entry.name)
        except OSError:
            continue
    return found


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _within(directory: str, cwd: str) -> bool:
    a, b = directory.rstrip("/"), cwd.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class OpencodeWatcher:
    key = "opencode"
    label = "opencode"

    def __init__(
        self,
        home: Path | None = None,
        proc: Path = Path("/proc"),
        now: Callable[[], float] = time.time,
    ) -> None:
        self.home = home or data_dir()
        self._proc = proc
        self._now = now
        self._stamp: tuple[tuple[int, int] | None, ...] = ()
        self._queried_at = float("-inf")
        self._busy: list[tuple[str, str, str]] = []

    def available(self) -> bool:
        return (self.home / "opencode.db").is_file()

    def working(self) -> tuple[AgentSession, ...]:
        # A step starts or finishes only by writing the database, so the query
        # is repeated only when it or its WAL changes, or a minute has passed.
        stamp = tuple(_stamp(self.home / n) for n in ("opencode.db", "opencode.db-wal"))
        if stamp != self._stamp or self._now() - self._queried_at >= REQUERY_AFTER:
            self._busy = self._busy_sessions()
            self._stamp, self._queried_at = stamp, self._now()
        busy = self._busy
        if not busy:
            return ()
        cwds = opencode_cwds(self._proc)
        found = []
        for _id, title, directory in busy:
            pid = next((p for cwd, p in cwds.items() if _within(directory, cwd)), None)
            if pid is None:
                continue
            name = title or Path(directory).name
            found.append(AgentSession(self.label, pid, name, "working", directory))
        return tuple(found)

    def _busy_sessions(self) -> list[tuple[str, str, str]]:
        db = self.home / "opencode.db"
        if not db.is_file():
            return []
        since = self._now() - STALE_AFTER
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            if "session_v2" in tables:
                rows = [
                    (a, b, c)
                    for a, b, c, busy in conn.execute(WORKING_V2)
                    # Seconds or milliseconds since the epoch.
                    if (busy / 1000 if busy > 1e11 else busy) >= since
                ]
            else:
                rows = list(conn.execute(WORKING_V1, (int(since * 1000),)))
        finally:
            conn.close()
        return [(str(a), str(b or ""), str(c or "")) for a, b, c in rows]
