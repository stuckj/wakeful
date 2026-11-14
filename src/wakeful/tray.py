"""The StatusNotifierItem: icon, tooltip and menu.

Left click starts a block of the last length used, or turns a running one off.
Everything else is in the menu. The menu is built once and only its texts,
visibility and check states change, so it does not rebuild under an open menu
every time the remaining time ticks down.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QObject, QSettings
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QInputDialog, QMenu, QSystemTrayIcon

from . import autostart
from .block import Block, Mode, describe_duration, describe_remaining, parse_duration
from .claude import ClaudeSession
from .controller import Controller, Status

log = logging.getLogger(__name__)

ICONS = Path(__file__).parent / "icons"
PRESETS: tuple[float, ...] = (30 * 60, 60 * 60, 2 * 3600, 4 * 3600)
EXTENSIONS: tuple[float, ...] = (15 * 60, 30 * 60, 60 * 60)
DEFAULT_DURATION = 60 * 60


def claude_count(n: int) -> str:
    return f"{n} session working" if n == 1 else f"{n} sessions working"


class Settings:
    """Preferences kept in ~/.config/wakeful/wakeful.conf."""

    def __init__(self) -> None:
        self._s = QSettings("wakeful", "wakeful")

    def _bool(self, key: str, default: bool) -> bool:
        return str(self._s.value(key, default)).lower() in ("true", "1")

    @property
    def mode(self) -> Mode:
        try:
            return Mode(self._s.value("mode", Mode.SLEEP.value))
        except ValueError:
            return Mode.SLEEP

    @mode.setter
    def mode(self, mode: Mode) -> None:
        self._s.setValue("mode", mode.value)

    @property
    def last_duration(self) -> float | None:
        """None means 'until turned off'."""
        try:
            seconds = float(self._s.value("last_duration", DEFAULT_DURATION))
        except (TypeError, ValueError):
            return DEFAULT_DURATION
        return None if seconds <= 0 else seconds

    @last_duration.setter
    def last_duration(self, seconds: float | None) -> None:
        self._s.setValue("last_duration", 0 if seconds is None else int(seconds))

    @property
    def ignore_lid(self) -> bool:
        return self._bool("ignore_lid", False)

    @ignore_lid.setter
    def ignore_lid(self, value: bool) -> None:
        self._s.setValue("ignore_lid", value)

    @property
    def watch_claude(self) -> bool:
        return self._bool("watch_claude", False)

    @watch_claude.setter
    def watch_claude(self, value: bool) -> None:
        self._s.setValue("watch_claude", value)

    @property
    def notify(self) -> bool:
        return self._bool("notify", True)

    @notify.setter
    def notify(self, value: bool) -> None:
        self._s.setValue("notify", value)


class Tray(QObject):
    def __init__(
        self,
        controller: Controller,
        settings: Settings,
        lid_available: bool,
        claude_available: bool = False,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._icon_idle = QIcon(str(ICONS / "wakeful-idle.svg"))
        self._icon_active = QIcon(str(ICONS / "wakeful-active.svg"))
        self._icon_active_screen = QIcon(str(ICONS / "wakeful-active-screen.svg"))

        self.icon = QSystemTrayIcon(self._icon_idle)
        self.icon.activated.connect(self._activated)
        self._menu = QMenu()
        self._claude_shown: tuple[ClaudeSession, ...] = ()
        self._build_menu(lid_available, claude_available)
        self.icon.setContextMenu(self._menu)

        controller.on_changed = self.update
        controller.on_ended = self._ended
        self.update(controller.status())
        self.icon.show()

    # -- menu --------------------------------------------------------------

    def _build_menu(self, lid_available: bool, claude_available: bool) -> None:
        m = self._menu
        self._status_action = m.addAction("")
        self._status_action.setEnabled(False)
        self._claude_menu = m.addMenu("")
        self._claude_menu.menuAction().setVisible(False)
        m.addSeparator()

        self._start_menu = m.addMenu("Keep awake for")
        for seconds in PRESETS:
            self._start_menu.addAction(describe_duration(seconds)).triggered.connect(
                lambda _=False, s=seconds: self._start(s)
            )
        self._start_menu.addAction("Custom…").triggered.connect(self._start_custom)
        self._start_menu.addSeparator()
        self._start_menu.addAction("Until I turn it off").triggered.connect(
            lambda: self._start(None)
        )

        self._extend_menu = m.addMenu("Add time")
        for seconds in EXTENSIONS:
            self._extend_menu.addAction(f"+{describe_duration(seconds)}").triggered.connect(
                lambda _=False, s=seconds: self._extend(s)
            )
        self._extend_menu.addAction("Custom…").triggered.connect(self._extend_custom)
        self._stop_action = m.addAction("Turn off")
        self._stop_action.triggered.connect(self._stop)
        m.addSeparator()

        self._mode_group = QActionGroup(self)
        self._mode_actions: dict[Mode, QAction] = {}
        for mode in Mode:
            action = m.addAction(mode.label)
            action.setCheckable(True)
            action.setActionGroup(self._mode_group)
            action.triggered.connect(lambda _=False, md=mode: self._set_mode(md))
            self._mode_actions[mode] = action
        self._mode_actions[self._settings.mode].setChecked(True)
        m.addSeparator()

        self._lid_action = m.addAction("Also ignore closing the lid")
        self._lid_action.setCheckable(True)
        self._lid_action.setChecked(lid_available and self._settings.ignore_lid)
        self._lid_action.setVisible(lid_available)
        self._lid_action.toggled.connect(self._set_ignore_lid)

        self._claude_action = m.addAction("Keep awake while Claude is working")
        self._claude_action.setCheckable(True)
        self._claude_action.setChecked(claude_available and self._settings.watch_claude)
        self._claude_action.setVisible(claude_available)
        self._claude_action.toggled.connect(self._set_watch_claude)

        self._notify_action = m.addAction("Notify when time runs out")
        self._notify_action.setCheckable(True)
        self._notify_action.setChecked(self._settings.notify)
        self._notify_action.toggled.connect(self._set_notify)

        self._autostart_action = m.addAction("Start at login")
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(autostart.enabled())
        self._autostart_action.toggled.connect(self._set_autostart)
        m.addSeparator()
        m.addAction("Quit").triggered.connect(self._quit)

    def update(self, status: Status) -> None:
        timed = status.active and status.remaining is not None
        self._status_action.setText(status.text)
        self._extend_menu.menuAction().setVisible(timed)
        self._stop_action.setVisible(status.active)
        # A block started over D-Bus can be in a mode other than the saved one;
        # once it ends, show the mode the next click will use.
        self._mode_actions[status.mode or self._settings.mode].setChecked(True)
        if self._lid_action.isVisible() and self._lid_action.isChecked() != status.ignore_lid:
            self._lid_action.blockSignals(True)
            self._lid_action.setChecked(status.ignore_lid)
            self._lid_action.blockSignals(False)
        if self._claude_action.isChecked() != status.watch_claude:
            self._claude_action.blockSignals(True)
            self._claude_action.setChecked(status.watch_claude)
            self._claude_action.blockSignals(False)
        self._show_claude(status.claude)

        working = claude_count(len(status.claude))
        if not status.active:
            last = describe_duration(self._settings.last_duration)
            if status.claude:
                self.icon.setIcon(self._icon_active)
                self.icon.setToolTip(
                    f"Wakeful: Claude is working\n{working}\nClick to keep awake for {last}"
                )
            else:
                self.icon.setIcon(self._icon_idle)
                self.icon.setToolTip(f"Wakeful: sleep allowed\nClick to keep awake for {last}")
            return
        self.icon.setIcon(
            self._icon_active_screen if status.mode is Mode.SCREEN else self._icon_active
        )
        assert status.mode is not None
        lines = [f"Wakeful: {describe_remaining(status.remaining)}", status.mode.label]
        if status.lid_ignored:
            lines.append("Closing the lid will not sleep")
        if status.claude:
            lines.append(f"Claude: {working}")
        lines.append("Click to turn off")
        self.icon.setToolTip("\n".join(lines))

    def _show_claude(self, sessions: tuple[ClaudeSession, ...]) -> None:
        """List the working sessions, rebuilding the submenu only when they change."""
        if sessions == self._claude_shown:
            return
        self._claude_shown = sessions
        menu = self._claude_menu
        menu.clear()
        menu.setTitle(f"Claude is working ({len(sessions)})")
        menu.menuAction().setVisible(bool(sessions))
        for s in sessions:
            action = menu.addAction(f"{s.name} — {s.doing}")
            action.setToolTip(s.cwd)
            action.setEnabled(False)

    # -- actions -----------------------------------------------------------

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self._controller.status().active:
            self._stop()
        else:
            self._start(self._settings.last_duration)

    def _start(self, seconds: float | None) -> None:
        self._settings.last_duration = seconds
        self._guard(lambda: self._controller.start(seconds, self._settings.mode))

    def _extend(self, seconds: float) -> None:
        self._guard(lambda: self._controller.extend(seconds))

    def _stop(self) -> None:
        self._controller.stop()

    def _set_mode(self, mode: Mode) -> None:
        self._settings.mode = mode
        self._guard(lambda: self._controller.set_mode(mode))

    def _set_ignore_lid(self, value: bool) -> None:
        self._settings.ignore_lid = value
        self._guard(lambda: setattr(self._controller, "ignore_lid", value))

    def _set_watch_claude(self, value: bool) -> None:
        self._settings.watch_claude = value
        self._guard(lambda: setattr(self._controller, "watch_claude", value))

    def _set_notify(self, value: bool) -> None:
        self._settings.notify = value

    def _set_autostart(self, value: bool) -> None:
        try:
            autostart.set_enabled(value)
        except OSError as e:
            self._error(f"Could not change start at login: {e}")

    def _ask_duration(self, title: str) -> float | bool | None:
        """Seconds, None for 'until turned off', or False if cancelled."""
        text, ok = QInputDialog.getText(
            None, title, "Duration (for example 90, 45m, 1h30m or 'forever'):"
        )
        if not ok or not text.strip():
            return False
        try:
            return parse_duration(text)
        except ValueError as e:
            self._error(str(e))
            return False

    def _start_custom(self) -> None:
        seconds = self._ask_duration("Keep awake for")
        if seconds is not False:
            self._start(seconds)  # type: ignore[arg-type]

    def _extend_custom(self) -> None:
        seconds = self._ask_duration("Add time")
        if seconds is None:
            self._error("Add time needs a length; use “Until I turn it off” instead.")
        elif seconds is not False:
            self._extend(seconds)  # type: ignore[arg-type]

    def _quit(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.quit()

    # -- feedback ----------------------------------------------------------

    def _guard(self, action) -> None:  # type: ignore[no-untyped-def]
        try:
            action()
        except Exception as e:
            log.exception("action failed")
            self._error(f"Could not keep the computer awake: {e}")
        self.update(self._controller.status())

    def _error(self, text: str) -> None:
        self.icon.showMessage("Wakeful", text, QSystemTrayIcon.MessageIcon.Warning, 10000)

    def _ended(self, block: Block, why: str) -> None:
        if why == "expired" and self._settings.notify:
            if self._controller.status().claude:
                text = "Time is up. Still keeping awake while Claude is working."
            else:
                text = "Time is up. The computer can sleep again."
            self.icon.showMessage("Wakeful", text, self._icon_idle, 10000)
