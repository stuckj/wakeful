"""Coding agents whose activity can keep the computer awake.

Qt-free. Each agent has a watcher that says whether the agent is installed and
which of its sessions are working right now; the controller polls them.
"""

from __future__ import annotations

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


def proc_start(pid: int) -> int | None:
    """The start time of pid in clock ticks since boot, or None if it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name in field 2 may contain spaces and parentheses.
    fields = stat[stat.rfind(")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None
