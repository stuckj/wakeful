"""The block being held: its mode, when it ends, and how that reads to a person.

Pure Python with no Qt or D-Bus, so the timer rules are testable on their own.
Times are seconds on a clock that keeps counting through suspend (see clock.py).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass


class Mode(enum.Enum):
    """What a block keeps awake."""

    SLEEP = "sleep"
    """Idle sleep is blocked; the screen still dims, turns off and locks."""

    SCREEN = "screen"
    """Idle sleep, screen dimming, screen-off and screen locking are all blocked."""

    @property
    def label(self) -> str:
        if self is Mode.SLEEP:
            return "Keep awake, screen can turn off"
        return "Keep awake and screen on"


@dataclass(frozen=True)
class Block:
    mode: Mode
    ends_at: float | None
    """None means the block lasts until it is turned off."""

    def remaining(self, now: float) -> float | None:
        """Seconds left, never negative; None for a block with no end."""
        if self.ends_at is None:
            return None
        return max(0.0, self.ends_at - now)

    def expired(self, now: float) -> bool:
        return self.ends_at is not None and now >= self.ends_at


class Session:
    """At most one block at a time, with the operations the tray offers."""

    def __init__(self) -> None:
        self.block: Block | None = None

    @property
    def active(self) -> bool:
        return self.block is not None

    def start(self, duration: float | None, mode: Mode, now: float) -> Block:
        """Start a block, replacing any running one. None runs until turned off."""
        if duration is not None and duration <= 0:
            raise ValueError("duration must be positive")
        ends_at = None if duration is None else now + duration
        self.block = Block(mode, ends_at)
        return self.block

    def extend(self, duration: float, now: float) -> Block:
        """Add time to the running block.

        A block that has run out but not yet been reaped is extended from now,
        not from its old end, so extending never yields a block that is already
        over. A block with no end is left as it is.
        """
        if duration <= 0:
            raise ValueError("duration must be positive")
        if self.block is None:
            raise RuntimeError("no block is running")
        if self.block.ends_at is not None:
            self.block = Block(self.block.mode, max(self.block.ends_at, now) + duration)
        return self.block

    def set_mode(self, mode: Mode) -> None:
        if self.block is not None:
            self.block = Block(mode, self.block.ends_at)

    def cancel(self) -> Block | None:
        ended, self.block = self.block, None
        return ended

    def reap(self, now: float) -> Block | None:
        """End the block if its time is up, returning the block that ended."""
        if self.block is not None and self.block.expired(now):
            return self.cancel()
        return None


def describe_remaining(seconds: float | None) -> str:
    """'47 min left', '2 h 5 min left', 'until turned off'.

    Rounded up, so the text never claims less time than is left and reads
    '1 min left' for the whole final minute.
    """
    if seconds is None:
        return "until turned off"
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min left"
    if hours:
        return f"{hours} h left"
    return f"{minutes} min left"


def describe_duration(seconds: float | None) -> str:
    """'30 minutes', '1 hour', '1 h 30 min', 'until turned off'."""
    if seconds is None:
        return "until turned off"
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} hour" + ("s" if hours > 1 else "")
    return f"{minutes} minute" + ("s" if minutes > 1 else "")


_UNTIL_OFF = ("forever", "indefinite", "until-off", "untiloff", "off", "inf")
_UNITS = {"h": 3600, "m": 60, "s": 1}
# Durations travel over D-Bus as int32 seconds.
MAX_DURATION = 2**31 - 1


def parse_duration(text: str) -> float | None:
    """Parse '90', '90m', '1h30m', '2h' or '45s' into seconds.

    A bare number is minutes. 'forever', 'until-off' and friends mean no end
    and return None.
    """
    t = text.strip().lower().replace(" ", "")
    if t in _UNTIL_OFF:
        return None
    if not t:
        raise ValueError("empty duration")
    if t.isdigit():
        seconds = int(t) * 60
    else:
        seconds = 0
        number = ""
        for ch in t:
            if ch.isdigit():
                number += ch
            elif ch in _UNITS and number:
                seconds += int(number) * _UNITS[ch]
                number = ""
            else:
                raise ValueError(f"cannot parse duration {text!r}")
        if number:
            raise ValueError(f"cannot parse duration {text!r}: number without a unit")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    if seconds > MAX_DURATION:
        raise ValueError("duration is too long; use 'forever' for no end")
    return float(seconds)
