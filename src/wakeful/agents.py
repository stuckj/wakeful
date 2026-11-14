"""Coding agents whose activity can keep the computer awake.

Qt-free. Each agent has a watcher that says whether the agent is installed and
which of its sessions are working right now; the controller polls them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LABEL_MAX = 60


@dataclass(frozen=True)
class AgentSession:
    agent: str
    """The agent's display name, e.g. 'Claude Code'."""
    pid: int
    name: str
    doing: str
    """'working' or 'running a shell'."""
    cwd: str

    @property
    def label(self) -> str:
        """The name, shortened for display: an untitled session may be named after its prompt."""
        if len(self.name) <= LABEL_MAX:
            return self.name
        return self.name[: LABEL_MAX - 1].rstrip() + "…"


class AgentWatcher(Protocol):
    key: str
    """Stable identifier used in settings and on the command line, e.g. 'claude'."""
    label: str

    def available(self) -> bool:
        """The agent is installed for this user."""

    def working(self) -> tuple[AgentSession, ...]:
        """Its sessions that are working now. May raise; the caller treats that as none."""


CLOCK_SLACK = 30.0
"""Seconds of disagreement allowed between a process's start time, derived from
the boot time, and the wall-clock times an agent records. /proc/stat's boot
time moves when the clock is stepped, as NTP does soon after boot."""


def boot_time(proc: Path = Path("/proc")) -> float:
    for line in (proc / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise OSError("no btime in /proc/stat")


def started_at(pid: int, proc: Path = Path("/proc")) -> float | None:
    """When pid started, in seconds since the epoch, or None if it is gone."""
    ticks = proc_start(pid, proc)
    if ticks is None:
        return None
    return boot_time(proc) + ticks / os.sysconf("SC_CLK_TCK")


def proc_start(pid: int, proc: Path = Path("/proc")) -> int | None:
    """The start time of pid in clock ticks since boot, or None if it is gone."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name in field 2 may contain spaces and parentheses.
    fields = stat[stat.rfind(")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None
