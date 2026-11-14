"""Ties the session to the things that act on it: the inhibitor and the lid guard.

Qt-free. The tray and the D-Bus control interface call in; a periodic tick()
reaps an expired block, polls for working Claude Code sessions, and keeps the
inhibition's reason text current.

Two things can ask for the inhibition: the block the user started and, with the
option on, any Claude Code session that is working. One inhibition serves both,
in the block's mode while there is a block and sleep-only otherwise. The lid
follows the block alone.
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
    def working(self) -> Sequence[ClaudeSession]: ...


@dataclass(frozen=True)
class Status:
    active: bool
    """A block the user started is running."""
    mode: Mode | None
    remaining: float | None
    """Seconds left; None when inactive or when the block has no end."""
    text: str
    """'Keeping awake: 47 min left', 'Keeping awake: Claude is working', or
    'Not keeping awake'."""
    lid_ignored: bool
    """The lid action is set to "Do nothing" right now."""
    ignore_lid: bool
    """The option is on: the lid will be ignored whenever a block runs."""
    watch_claude: bool = False
    """The option is on: working Claude Code sessions keep the computer awake."""
    claude: tuple[ClaudeSession, ...] = ()
    """The working sessions keeping it awake; empty while the option is off."""

    @property
    def held(self) -> bool:
        """Something is keeping the computer awake."""
        return self.active or bool(self.claude)


CLAUDE_NAMES_SHOWN = 3


def reason_text(remaining: float | None) -> str:
    return f"Keeping awake: {describe_remaining(remaining)}"


def claude_reason_text(sessions: Sequence[ClaudeSession]) -> str:
    names = [s.name for s in sessions[:CLAUDE_NAMES_SHOWN]]
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
        self._watch_claude = False
        self._claude: tuple[ClaudeSession, ...] = ()
        self.on_changed: Callable[[Status], None] = lambda status: None
        self.on_ended: Callable[[Block, str], None] = lambda block, why: None
        """Called with the block and 'expired' or 'stopped'."""

    # -- queries -----------------------------------------------------------

    def status(self) -> Status:
        block = self._session.block
        if block is None:
            return Status(
                False,
                None,
                None,
                "Keeping awake: Claude is working" if self._claude else "Not keeping awake",
                False,
                self._ignore_lid,
                self._watch_claude,
                self._claude,
            )
        remaining = block.remaining(self._now())
        return Status(
            True,
            block.mode,
            remaining,
            reason_text(remaining),
            self._lid_engaged,
            self._ignore_lid,
            self._watch_claude,
            self._claude,
        )

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

    @property
    def watch_claude(self) -> bool:
        return self._watch_claude

    @watch_claude.setter
    def watch_claude(self, value: bool) -> None:
        if self._claude_watcher is None and value:
            raise RuntimeError("Claude Code was not found")
        self._watch_claude = value
        self._poll_claude()
        self._hold_or_warn()
        self._changed()

    # -- operations --------------------------------------------------------

    def start(self, duration: float | None, mode: Mode) -> None:
        """Start (or restart) a block. Raises if the inhibition cannot be taken.

        On failure a block that was already running carries on unchanged.
        """
        previous = self._session.block
        self._session.start(duration, mode, self._now())
        try:
            self._apply()
        except Exception:
            self._session.block = previous
            if previous is None and not self._claude:
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
        """Reap an expired block, follow Claude Code, and refresh the reason text."""
        was_held = self._session.active or bool(self._claude)
        self._poll_claude()
        block = self._session.block
        if block is not None and block.expired(self._now()):
            self._end("expired")
            return
        if not was_held and not self._claude:
            return
        # Retried on every tick: hold() also re-takes an inhibition that was
        # lost, for example to a PowerDevil restart.
        self._hold_or_warn()
        self._changed()

    def shutdown(self) -> None:
        """Release everything before exit. The block is not reported as ended."""
        self._session.cancel()
        self._claude = ()
        self._inhibitor.release()
        self._sync_lid()

    # -- internals ---------------------------------------------------------

    def _apply(self) -> None:
        """Hold what the block and Claude Code ask for, or release if neither does."""
        block = self._session.block
        if block is not None:
            self._inhibitor.hold(block.mode, reason_text(block.remaining(self._now())))
        elif self._claude:
            self._inhibitor.hold(Mode.SLEEP, claude_reason_text(self._claude))
        else:
            self._inhibitor.release()

    def _hold_or_warn(self) -> None:
        try:
            self._apply()
        except Exception as e:
            log.warning("could not refresh the inhibition: %s", e)

    def _poll_claude(self) -> None:
        if not self._watch_claude or self._claude_watcher is None:
            self._claude = ()
            return
        try:
            self._claude = tuple(self._claude_watcher.working())
        except Exception:
            log.exception("could not read the Claude Code sessions")
            self._claude = ()

    def _end(self, why: str) -> None:
        ended = self._session.cancel()
        self._poll_claude()
        self._hold_or_warn()
        self._sync_lid()
        if ended is not None:
            self.on_ended(ended, why)
        self._changed()

    def _sync_lid(self) -> None:
        if self._lid is None:
            return
        want = self._ignore_lid and self._session.active
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
