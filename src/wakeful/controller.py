"""Ties the session to the things that act on it: the inhibitor and the lid guard.

Qt-free. The tray and the D-Bus control interface call in; a periodic tick()
reaps an expired block and keeps the inhibition's reason text current.

Two things can keep the computer awake, independently: the timed block the user
started, and, with agentic checks on, any watched coding agent that is working
(and for AGENT_GRACE seconds after the last one was seen). One inhibition serves
both, in the block's mode while there is a block and in the chosen mode
otherwise. The lid follows the inhibition. Agentic checks stay on while nothing
is working; they then hold nothing and the session's own idle timers apply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import clock
from .agents import AgentSession, AgentWatcher
from .block import Block, Mode, Session, describe_remaining

log = logging.getLogger(__name__)

AGENT_GRACE = 60.0
"""Seconds agentic checks keep holding after the last working session, so a
pause between two turns does not let the computer sleep."""


class Inhibitor(Protocol):
    def hold(self, mode: Mode, reason: str) -> None:
        """Hold an inhibition for mode with this reason, replacing any held one.

        If this raises, whatever was held before is still held.
        """

    def release(self) -> None:
        """Release everything held. Safe to call when nothing is."""


class LidGuard(Protocol):
    def engage(self) -> None: ...
    def restore(self) -> None: ...


@dataclass(frozen=True)
class Status:
    active: bool
    """A timed block (or one that lasts until turned off) is running."""
    mode: Mode
    """The block's mode, or the chosen mode when there is no block."""
    remaining: float | None
    """Seconds left; None when inactive or when the block has no end."""
    text: str
    """'Keeping awake: 47 min left', 'Keeping awake: agents working',
    'Watching agents', or 'Not keeping awake'."""
    lid_ignored: bool
    """The lid action is set to "Do nothing" right now."""
    ignore_lid: bool
    """The option is on: the lid will be ignored whenever the inhibition is held."""
    held: bool = False
    """The inhibition is held, for the block or for working agents."""
    agents_enabled: bool = False
    """The agentic checks master switch is on."""
    watched: tuple[str, ...] = ()
    """Keys of the agents ticked, whether or not the master switch is on."""
    watching: bool = False
    """Agentic checks are on and at least one ticked agent is installed."""
    sessions: tuple[AgentSession, ...] = ()
    """Working sessions of the ticked agents, read while a block runs or while watching."""


NAMES_SHOWN = 3


def reason_text(remaining: float | None) -> str:
    return f"Keeping awake: {describe_remaining(remaining)}"


def agents_reason_text(sessions: Sequence[AgentSession]) -> str:
    if not sessions:
        return "Keeping awake: agents working"
    names = [s.label for s in sessions[:NAMES_SHOWN]]
    if len(sessions) > NAMES_SHOWN:
        names.append(f"{len(sessions) - NAMES_SHOWN} more")
    return f"Agents working: {', '.join(names)}"


class Controller:
    def __init__(
        self,
        inhibitor: Inhibitor,
        lid: LidGuard | None = None,
        now: Callable[[], float] = clock.now,
        agents: Iterable[AgentWatcher] = (),
    ) -> None:
        self._inhibitor = inhibitor
        self._lid = lid
        self._now = now
        self._watchers = {w.key: w for w in agents}
        self._session = Session()
        self._mode = Mode.SLEEP
        self._ignore_lid = False
        self._lid_engaged = False
        self._agents_enabled = False
        self._watched: set[str] = set()
        self._sessions: tuple[AgentSession, ...] = ()
        self._seen_at: float | None = None
        self._held = False
        """Whether the last successful _apply() left the inhibition held."""
        self.on_changed: Callable[[Status], None] = lambda status: None
        self.on_ended: Callable[[Block, str], None] = lambda block, why: None
        """Called with the block and 'expired' or 'stopped'."""

    # -- queries -----------------------------------------------------------

    def status(self) -> Status:
        block = self._session.block
        remaining = block.remaining(self._now()) if block else None
        if block is not None:
            text = reason_text(remaining)
        elif self._held:
            text = "Keeping awake: agents working"
        elif self._watching():
            text = "Watching agents"
        else:
            text = "Not keeping awake"
        return Status(
            active=block is not None,
            mode=block.mode if block else self._mode,
            remaining=remaining,
            text=text,
            lid_ignored=self._lid_engaged,
            ignore_lid=self._ignore_lid,
            held=self._held,
            agents_enabled=self._agents_enabled,
            watched=tuple(k for k in self._watchers if k in self._watched),
            watching=self._watching(),
            sessions=self._sessions,
        )

    def agents(self) -> list[tuple[str, str, bool]]:
        """(key, label, installed) for every agent Wakeful knows."""
        return [(w.key, w.label, w.available()) for w in self._watchers.values()]

    @property
    def ignore_lid(self) -> bool:
        return self._ignore_lid

    @ignore_lid.setter
    def ignore_lid(self, value: bool) -> None:
        if self._lid is None and value:
            raise RuntimeError("lid handling is not available")
        self._ignore_lid = value
        self._sync_lid()
        self._changed()

    # -- operations --------------------------------------------------------

    def start(self, duration: float | None, mode: Mode) -> None:
        """Start (or restart) a block. Raises if the inhibition cannot be taken.

        On failure a block that was already running carries on unchanged.
        """
        previous = self._session.block
        self._session.start(duration, mode, self._now())
        self._poll()
        try:
            self._apply()
        except Exception:
            self._session.block = previous
            if not self._held:
                self._inhibitor.release()
            self._changed()
            raise
        self._changed()

    def extend(self, duration: float) -> None:
        self._session.extend(duration, self._now())
        self._apply()
        self._changed()

    def set_mode(self, mode: Mode) -> None:
        """Choose the mode for the block, if any, and for agentic holds."""
        block = self._session.block
        previous = self._mode
        self._mode = mode
        if block is not None:
            self._session.set_mode(mode)
        try:
            self._apply()
        except Exception:
            self._mode = previous
            if block is not None:
                self._session.set_mode(block.mode)
            raise
        self._changed()

    def stop(self) -> None:
        """End the block. Agentic checks carry on."""
        self._end("stopped")

    def set_agents_enabled(self, value: bool) -> None:
        self._agents_enabled = value
        self._refresh()

    def set_watched(self, key: str, value: bool) -> None:
        if key not in self._watchers:
            raise ValueError(f"unknown agent {key!r}; known: {', '.join(self._watchers)}")
        if value:
            self._watched.add(key)
        else:
            self._watched.discard(key)
        self._refresh()

    def tick(self) -> None:
        """Reap an expired block, and refresh the sessions and the inhibition."""
        block = self._session.block
        if block is None and not self._watching() and not self._held:
            return
        if block is not None and block.expired(self._now()):
            self._end("expired")
            return
        self._refresh()

    def shutdown(self) -> None:
        """Release everything before exit. The block is not reported as ended."""
        self._session.cancel()
        self._sessions, self._held = (), False
        self._inhibitor.release()
        self._sync_lid()

    # -- internals ---------------------------------------------------------

    def _watching(self) -> bool:
        return self._agents_enabled and any(self._watchers[k].available() for k in self._watched)

    def _wanted(self) -> bool:
        if self._session.block is not None:
            return True
        if not self._watching():
            return False
        if self._sessions:
            return True
        return self._seen_at is not None and self._now() - self._seen_at < AGENT_GRACE

    def _poll(self) -> None:
        """Read the working sessions of the ticked agents, when anything needs them."""
        if self._session.block is None and not self._watching():
            self._sessions, self._seen_at = (), None
            return
        found: list[AgentSession] = []
        for key, watcher in self._watchers.items():
            if key not in self._watched or not watcher.available():
                continue
            try:
                found.extend(watcher.working())
            except Exception:
                log.exception("could not read the %s sessions", watcher.label)
        self._sessions = tuple(found)
        if self._sessions and self._watching():
            self._seen_at = self._now()

    def _refresh(self) -> None:
        self._poll()
        # Retried on every tick: hold() also re-takes an inhibition that was
        # lost, for example to a PowerDevil restart.
        try:
            self._apply()
        except Exception as e:
            log.warning("could not refresh the inhibition: %s", e)
        self._changed()

    def _apply(self) -> None:
        """Hold what is wanted right now, or release it, and move the lid with it."""
        wanted = self._wanted()
        block = self._session.block
        if not wanted:
            self._inhibitor.release()
        elif block is not None:
            self._inhibitor.hold(block.mode, reason_text(block.remaining(self._now())))
        else:
            self._inhibitor.hold(self._mode, agents_reason_text(self._sessions))
        if wanted != self._held:
            self._held = wanted
            self._sync_lid()

    def _end(self, why: str) -> None:
        ended = self._session.cancel()
        self._poll()
        try:
            self._apply()
        except Exception as e:
            # Holding for agents failed; the block's own inhibition must still go.
            log.warning("could not hold for working agents: %s", e)
            self._inhibitor.release()
            self._held = False
            self._sync_lid()
        if ended is not None:
            self.on_ended(ended, why)
        self._changed()

    def _sync_lid(self) -> None:
        if self._lid is None:
            return
        want = self._ignore_lid and self._held
        if want == self._lid_engaged:
            return
        try:
            if want:
                self._lid.engage()
            else:
                self._lid.restore()
            self._lid_engaged = want
        except Exception:
            log.exception("could not %s the lid action", "change" if want else "restore")

    def _changed(self) -> None:
        self.on_changed(self.status())
