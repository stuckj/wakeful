"""Ties the session to the things that act on it: the inhibitor and the lid guard.

Qt-free. The tray and the D-Bus control interface call in; a periodic tick()
reaps an expired block and keeps the inhibition's reason text current.

While a block runs, tick() also reads which Claude Code sessions are working,
so they can be shown. A block that lasts while Claude works runs until it is
turned off, but holds the inhibition (and the lid) only while a session is
working and for CLAUDE_GRACE seconds after the last one was seen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import clock
from .block import Block, Mode, Session, describe_remaining
from .claude import ClaudeSession

log = logging.getLogger(__name__)

CLAUDE_GRACE = 60.0
"""Seconds a while-Claude block keeps holding after the last working session,
so a pause between two turns does not let the computer sleep."""


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


class ClaudeWatcher(Protocol):
    def available(self) -> bool: ...
    def working(self) -> Sequence[ClaudeSession]: ...


@dataclass(frozen=True)
class Status:
    active: bool
    """A block is running."""
    mode: Mode | None
    remaining: float | None
    """Seconds left; None when inactive or when the block has no end."""
    text: str
    """'Keeping awake: 47 min left', 'Keeping awake while Claude is working',
    'Sleep allowed until Claude is working', or 'Not keeping awake'."""
    lid_ignored: bool
    """The lid action is set to "Do nothing" right now."""
    ignore_lid: bool
    """The option is on: the lid will be ignored whenever a block holds."""
    while_claude: bool = False
    """The block lasts while a Claude Code session is working."""
    claude: tuple[ClaudeSession, ...] = ()
    """The Claude Code sessions working while a block runs; empty otherwise."""
    held: bool = False
    """The inhibition is held: always during a timed block, and during a
    while-Claude block only while Claude is working."""


CLAUDE_NAMES_SHOWN = 3
WHILE_CLAUDE_TEXT = "Keeping awake while Claude is working"
CLAUDE_IDLE_TEXT = "Sleep allowed until Claude is working"


def reason_text(remaining: float | None) -> str:
    return f"Keeping awake: {describe_remaining(remaining)}"


def claude_reason_text(sessions: Sequence[ClaudeSession]) -> str:
    if not sessions:
        return WHILE_CLAUDE_TEXT
    names = [s.label for s in sessions[:CLAUDE_NAMES_SHOWN]]
    if len(sessions) > CLAUDE_NAMES_SHOWN:
        names.append(f"{len(sessions) - CLAUDE_NAMES_SHOWN} more")
    return f"Claude is working: {', '.join(names)}"


class Controller:
    def __init__(
        self,
        inhibitor: Inhibitor,
        lid: LidGuard | None = None,
        now: Callable[[], float] = clock.now,
        claude: ClaudeWatcher | None = None,
    ) -> None:
        self._inhibitor = inhibitor
        self._lid = lid
        self._now = now
        self._claude_watcher = claude
        self._session = Session()
        self._ignore_lid = False
        self._lid_engaged = False
        self._claude: tuple[ClaudeSession, ...] = ()
        self._claude_seen_at: float | None = None
        self._held = False
        """What the last _apply() decided, so tick() can see holding change."""
        self.on_changed: Callable[[Status], None] = lambda status: None
        self.on_ended: Callable[[Block, str], None] = lambda block, why: None
        """Called with the block and 'expired' or 'stopped'."""

    # -- queries -----------------------------------------------------------

    def status(self) -> Status:
        block = self._session.block
        if block is None:
            return Status(False, None, None, "Not keeping awake", False, self._ignore_lid)
        remaining = block.remaining(self._now())
        held = self._holding()
        if not block.while_claude:
            text = reason_text(remaining)
        else:
            text = WHILE_CLAUDE_TEXT if held else CLAUDE_IDLE_TEXT
        return Status(
            True,
            block.mode,
            remaining,
            text,
            self._lid_engaged,
            self._ignore_lid,
            block.while_claude,
            self._claude,
            held,
        )

    @property
    def claude_available(self) -> bool:
        return self._claude_watcher is not None and self._claude_watcher.available()

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

    def start(self, duration: float | None, mode: Mode, while_claude: bool = False) -> None:
        """Start (or restart) a block. Raises if the inhibition cannot be taken,
        or, for a block that lasts while Claude works, if Claude Code is missing.

        On failure a block that was already running carries on unchanged.
        """
        if while_claude and not self.claude_available:
            raise RuntimeError("Claude Code was not found")
        sessions = self._read_claude()
        previous = self._session.block, self._claude, self._claude_seen_at, self._held
        self._session.start(duration, mode, self._now(), while_claude)
        self._claude = sessions
        if sessions:
            self._claude_seen_at = self._now()
        try:
            self._apply()
        except Exception:
            self._session.block, self._claude, self._claude_seen_at, self._held = previous
            if self._session.block is None:
                self._inhibitor.release()
            self._sync_lid()
            self._changed()
            raise
        self._sync_lid()
        self._changed()

    def extend(self, duration: float) -> None:
        self._session.extend(duration, self._now())
        self._apply()
        self._changed()

    def set_mode(self, mode: Mode) -> None:
        block = self._session.block
        if block is None or block.mode is mode:
            return
        self._session.set_mode(mode)
        try:
            self._apply()
        except Exception:
            self._session.set_mode(block.mode)
            raise
        self._changed()

    def stop(self) -> None:
        self._end("stopped")

    def tick(self) -> None:
        """Reap an expired block, or refresh the sessions and inhibition of a running one."""
        block = self._session.block
        if block is None:
            return
        now = self._now()
        self._claude = self._read_claude()
        if self._claude:
            self._claude_seen_at = now
        if block.expired(now):
            self._end("expired")
            return
        was_holding = self._held
        # Retried on every tick: hold() also re-takes an inhibition that was
        # lost, for example to a PowerDevil restart.
        try:
            self._apply()
        except Exception as e:
            log.warning("could not refresh the inhibition: %s", e)
        if self._held != was_holding:
            self._sync_lid()
        self._changed()

    def shutdown(self) -> None:
        """Release everything before exit. The block is not reported as ended."""
        self._session.cancel()
        self._claude = ()
        self._inhibitor.release()
        self._sync_lid()

    # -- internals ---------------------------------------------------------

    def _holding(self) -> bool:
        block = self._session.block
        if block is None:
            return False
        if not block.while_claude or self._claude:
            return True
        seen = self._claude_seen_at
        return seen is not None and self._now() - seen < CLAUDE_GRACE

    def _apply(self) -> None:
        """Hold the inhibition the block asks for right now, or release it."""
        block = self._session.block
        assert block is not None
        self._held = self._holding()
        if not self._held:
            self._inhibitor.release()
        elif block.while_claude:
            self._inhibitor.hold(block.mode, claude_reason_text(self._claude))
        else:
            self._inhibitor.hold(block.mode, reason_text(block.remaining(self._now())))

    def _read_claude(self) -> tuple[ClaudeSession, ...]:
        if self._claude_watcher is None:
            return ()
        try:
            return tuple(self._claude_watcher.working())
        except Exception:
            log.exception("could not read the Claude Code sessions")
            return ()

    def _end(self, why: str) -> None:
        ended = self._session.cancel()
        self._claude, self._claude_seen_at = (), None
        self._inhibitor.release()
        self._sync_lid()
        if ended is not None:
            self.on_ended(ended, why)
        self._changed()

    def _sync_lid(self) -> None:
        if self._lid is None:
            return
        want = self._ignore_lid and self._holding()
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
