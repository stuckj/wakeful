"""Optionally set the lid-close action to "Do nothing" while a block keeps the computer awake.

Inhibitions cannot stop the lid: PowerDevil treats closing it as an explicit
request and bypasses every policy. The only lever is the LidAction setting in
powerdevilrc, which persists, so it is changed with a journal:

1. Read the current value for every profile (AC, Battery, LowBattery).
2. Write those originals to the journal file and fsync it.
3. Only then set LidAction=0 and ask PowerDevil to reload.

Restoring writes each original back — deleting the key if it was unset — but
only where the value is still the 0 written here, so a change made in System
Settings meanwhile is kept. The journal is removed last. A journal found at
startup means a previous run never restored, and it is restored then.

The file is read and written with kreadconfig6/kwriteconfig6, the same KConfig
code PowerDevil and System Settings use, rather than parsed here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

CONFIG_FILE = "powerdevilrc"
PROFILES = ("AC", "Battery", "LowBattery")
GROUP = "SuspendAndShutdown"
KEY = "LidAction"
DO_NOTHING = "0"
# kreadconfig6 prints --default for a missing key, and LidAction is a number,
# so this cannot be mistaken for a real value.
_UNSET = "wakeful-unset"


class LidError(Exception):
    pass


class KConfigStore(Protocol):
    def read(self, profile: str) -> str | None:
        """The LidAction value for a profile, or None if the key is unset."""

    def write(self, profile: str, value: str | None) -> None:
        """Set the value; None deletes the key."""


class KConfigTools:
    """KConfigStore over kreadconfig6 / kwriteconfig6."""

    def __init__(self, file: str = CONFIG_FILE) -> None:
        self._file = file

    def _args(self, tool: str, profile: str) -> list[str]:
        return [tool, "--file", self._file, "--group", profile, "--group", GROUP, "--key", KEY]

    def read(self, profile: str) -> str | None:
        out = _run([*self._args("kreadconfig6", profile), "--default", _UNSET])
        value = out.rstrip("\n")
        return None if value == _UNSET else value

    def write(self, profile: str, value: str | None) -> None:
        args = self._args("kwriteconfig6", profile)
        _run([*args, "--delete"] if value is None else [*args, value])


def _run(args: list[str]) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LidError(f"{args[0]}: {e}") from e
    if done.returncode != 0:
        raise LidError(f"{args[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def default_journal_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(state) / "wakeful" / "lid-restore.json"


class LidGuard:
    def __init__(
        self,
        store: KConfigStore,
        reload: Callable[[], None],
        journal: Path | None = None,
    ) -> None:
        self._store = store
        self._reload = reload
        self.journal = journal if journal is not None else default_journal_path()

    @property
    def engaged(self) -> bool:
        return self.journal.exists()

    def engage(self) -> None:
        if self.engaged:
            # Already "Do nothing" from this or an unrestored earlier run; the
            # journal holds the true originals and must not be overwritten
            # with the 0s written by that run. An unreadable one raises here,
            # before anything changes, since nothing could restore after.
            self._read_journal()
            log.info("lid journal already present; keeping its originals")
        else:
            originals = {p: self._store.read(p) for p in PROFILES}
            self._write_journal(originals)
        try:
            for profile in PROFILES:
                self._store.write(profile, DO_NOTHING)
        except LidError:
            self.restore()
            raise
        self._reload_quietly()

    def restore(self) -> None:
        """Put back what the journal recorded. A no-op with no journal."""
        originals = self._read_journal()
        if originals is None:
            return
        for profile, original in originals.items():
            if profile not in PROFILES:
                continue
            current = self._store.read(profile)
            if current != DO_NOTHING:
                log.info("%s lid action is now %r, not ours; leaving it", profile, current)
                continue
            self._store.write(profile, original)
        # PowerDevil keeps "Do nothing" loaded until it reloads, so the journal
        # stays until it has; a repeat finds the values restored and only
        # reloads.
        try:
            self._reload()
        except Exception as e:
            raise LidError(f"PowerDevil did not reload its configuration: {e}") from e
        self.journal.unlink(missing_ok=True)

    def _write_journal(self, originals: dict[str, str | None]) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.journal.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"version": 1, "file": CONFIG_FILE, "originals": originals}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.journal)
        dir_fd = os.open(self.journal.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _read_journal(self) -> dict[str, str | None] | None:
        try:
            data = json.loads(self.journal.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            # Unreadable means the originals are unknown. Leave the file for a
            # person to look at rather than guess, and do not touch the config.
            raise LidError(f"cannot read {self.journal}: {e}") from e
        originals = data.get("originals") if isinstance(data, dict) else None
        if not isinstance(originals, dict):
            raise LidError(f"{self.journal} has no originals")
        return originals

    def _reload_quietly(self) -> None:
        try:
            self._reload()
        except Exception:
            log.exception("could not ask PowerDevil to reload its configuration")
