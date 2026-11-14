"""Which opencode sessions on this machine are working right now.

Qt-free. opencode (opencode.ai) keeps its working state in memory only, but its
SQLite database at $XDG_DATA_HOME/opencode/opencode.db (or opencode-<channel>.db,
or $OPENCODE_DB, for 2.x) shows the same. 2.x copies 1.x's sessions into its own
tables and keeps the old ones, so both kinds are read. Neither is a documented
interface:

  1.x  An assistant message is written when a step starts, with
       data.time.completed unset, and completed when the step and its tools
       finish. A session with such a message is working.
  2.x  session_v2.time_busy is set when a run starts and time_idle when it
       ends; the session is working while time_busy is the later.

The database records no process, and a crash leaves an unfinished step behind
for good. So a step counts only while an opencode process that started before
it runs in its session's directory (or below it), or as 2.x's background
service: one left by an earlier process is older than every process running
now. A 1.x step whose session has written nothing for SILENT_FOR is also taken
to be left behind, which covers a process killed while an older one runs in the
same directory. Nothing is read while no opencode process runs. The database is
opened read-only, and only the session, message, part and session_v2 tables are
read; the same file holds credentials.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agents import CLOCK_SLACK, AgentSession, boot_time, proc_start

REQUERY_AFTER = 60
RESCAN_AFTER = 30
"""Seconds between walks of /proc for new opencode processes; in between, only
the ones already found are checked."""
BINARIES = {"opencode", "opencode.exe"}

SILENT_FOR = 2 * 3600
"""Seconds without any write to a 1.x session after which its unfinished step is
taken to be left by a killed process. A single tool running silently for longer
stops counting."""

WORKING_V1 = """
    SELECT s.id, s.title, s.directory, max(m.time_created),
           max(max(m.time_updated),
               coalesce((SELECT max(p.time_updated) FROM part p WHERE p.session_id = s.id), 0))
    FROM message m JOIN session s ON s.id = m.session_id
    WHERE m.time_created >= ?
      AND json_extract(m.data, '$.role') = 'assistant'
      AND json_extract(m.data, '$.time.completed') IS NULL
    GROUP BY s.id
"""
WORKING_V2 = """
    SELECT id, title, directory, time_busy, time_busy FROM session_v2
    WHERE time_busy IS NOT NULL AND (time_idle IS NULL OR time_idle < time_busy)
"""


@dataclass(frozen=True)
class Process:
    pid: int
    cwd: str
    started: float
    """Seconds since the epoch."""
    service: bool = False
    """2.x's background service (`opencode serve --service`), which runs the sessions
    of every directory and keeps the cwd of whichever client started it."""


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode"


def opencode_processes(proc: Path = Path("/proc")) -> list[Process]:
    """Every opencode process this user can see, with its cwd and start time."""
    ticks = os.sysconf("SC_CLK_TCK")
    booted: float | None = None
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # An upgraded binary shows as "/path/opencode.exe (deleted)".
            exe = os.readlink(entry / "exe").removesuffix(" (deleted)")
            if Path(exe).name not in BINARIES:
                continue
            cwd = os.readlink(entry / "cwd")
            args = (entry / "cmdline").read_bytes().split(b"\0")[1:]
        except OSError:
            continue
        start = proc_start(int(entry.name), proc)
        if start is None:
            continue
        if booted is None:
            booted = boot_time(proc)
        service = b"serve" in args and b"--service" in args
        found.append(Process(int(entry.name), cwd, booted + start / ticks, service))
    return found


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _within(directory: str, cwd: str) -> bool:
    """cwd is the session's directory or below it; never a parent, such as ~ or /."""
    a, b = directory.rstrip("/"), cwd.rstrip("/")
    return bool(a) and (a == b or b.startswith(a + "/"))


def _seconds(t: float) -> float:
    """opencode times are milliseconds since the epoch; accept seconds too."""
    return t / 1000 if t > 1e11 else t


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
        self._key: tuple = ()
        self._queried_at = float("-inf")
        self._busy: list[tuple[str, str, str, float, float | None]] = []
        self._procs: list[Process] = []
        self._scanned_at = float("-inf")

    def databases(self) -> list[Path]:
        """opencode.db, and the per-channel opencode-<channel>.db of 2.x builds."""
        env = os.environ.get("OPENCODE_DB")
        found = [self.home / env] if env and env != ":memory:" else []
        found += [self.home / "opencode.db", *sorted(self.home.glob("opencode-*.db"))]
        return [db for db in dict.fromkeys(found) if db.is_file()]

    def available(self) -> bool:
        return bool(self.databases())

    def working(self) -> tuple[AgentSession, ...]:
        procs = self._processes()
        if not procs:
            return ()
        since = min(p.started for p in procs) - CLOCK_SLACK
        # A step starts or finishes only by writing the database, so the query
        # is repeated only when one changes, when the processes change, or once
        # a minute.
        dbs = self.databases()
        key = (since, *(_stamp(Path(f"{db}{suffix}")) for db in dbs for suffix in ("", "-wal")))
        if key != self._key or self._now() - self._queried_at >= REQUERY_AFTER:
            self._busy = [row for db in dbs for row in self._busy_sessions(db, since)]
            self._key, self._queried_at = key, self._now()
        found = []
        for _id, title, directory, step, last_write in self._busy:
            if last_write is not None and self._now() - last_write >= SILENT_FOR:
                continue
            owner = next(
                (
                    p
                    for p in procs
                    if (p.service or _within(directory, p.cwd)) and p.started - CLOCK_SLACK <= step
                ),
                None,
            )
            if owner is not None:
                name = title or Path(directory).name
                found.append(AgentSession(self.label, owner.pid, name, "working", directory))
        return tuple(found)

    def _processes(self) -> list[Process]:
        """The opencode processes, walking /proc only every RESCAN_AFTER seconds or
        when one already found has gone."""
        gone = any(proc_start(p.pid, self._proc) is None for p in self._procs)
        if gone or self._now() - self._scanned_at >= RESCAN_AFTER:
            self._procs = opencode_processes(self._proc)
            self._scanned_at = self._now()
        return self._procs

    def _busy_sessions(
        self, db: Path, since: float
    ) -> list[tuple[str, str, str, float, float | None]]:
        """Sessions with a step begun since `since`: when that step began, and,
        for 1.x, when the session was last written. 2.x keeps 1.x's tables
        beside its own, so both are read: either version may be the one running."""
        conn = sqlite3.connect(f"{db.absolute().as_uri()}?mode=ro", uri=True, timeout=0.5)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            rows = []
            if "session_v2" in tables:
                rows += [(a, b, c, t, None) for a, b, c, t, _ in conn.execute(WORKING_V2)]
            if {"session", "message", "part"} <= tables:
                rows += list(conn.execute(WORKING_V1, (int(since * 1000),)))
        finally:
            conn.close()
        return [
            (str(a), str(b or ""), str(c or ""), _seconds(t), None if w is None else _seconds(w))
            for a, b, c, t, w in rows
            if t is not None and _seconds(t) >= since
        ]
