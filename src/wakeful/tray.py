"""The StatusNotifierItem: icon, tooltip and menu.

The icon is a closed eye when nothing is enabled, a half-lidded eye while
agentic checks watch with nothing working, and an open eye while the computer
is kept awake. Left click steps through off, agentic checks, and agentic checks
plus a timer of the last length used. Everything else is in the menu. The menu
is built once and only its texts, visibility and check states change, so it
does not rebuild under an open menu every time the remaining time ticks down.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QObject, QSettings
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QInputDialog, QMenu, QSystemTrayIcon

from . import autostart
from .agents import AgentSession
from .block import (
    MAX_DURATION,
    Block,
    Mode,
    describe_duration,
    describe_grace,
    describe_remaining,
    parse_duration,
    parse_grace,
)
from .controller import AGENT_GRACE, Controller, Status

log = logging.getLogger(__name__)

ICONS = Path(__file__).parent / "icons"
PRESETS: tuple[float, ...] = (30 * 60, 60 * 60, 2 * 3600, 4 * 3600)
GRACE_PRESETS: tuple[float, ...] = (0, 60, 2 * 60, 5 * 60, 15 * 60)
EXTENSIONS: tuple[float, ...] = (15 * 60, 30 * 60, 60 * 60)
DEFAULT_DURATION = 60 * 60


def session_count(n: int) -> str:
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
    def agents_enabled(self) -> bool:
        return self._bool("agents_enabled", False)

    @agents_enabled.setter
    def agents_enabled(self, value: bool) -> None:
        self._s.setValue("agents_enabled", value)

    def watched(self, agent: str) -> bool:
        return self._bool(f"watch_{agent}", True)

    def set_watched(self, agent: str, value: bool) -> None:
        self._s.setValue(f"watch_{agent}", value)

    @property
    def agent_grace(self) -> float:
        """Seconds to keep awake after agents stop working."""
        try:
            seconds = float(self._s.value("agent_grace", AGENT_GRACE))
        except (TypeError, ValueError):
            return AGENT_GRACE
        return seconds if 0 <= seconds <= MAX_DURATION else AGENT_GRACE

    @agent_grace.setter
    def agent_grace(self, seconds: float) -> None:
        self._s.setValue("agent_grace", int(seconds))

    @property
    def notify(self) -> bool:
        return self._bool("notify", True)

    @notify.setter
    def notify(self, value: bool) -> None:
        self._s.setValue("notify", value)


class Tray(QObject):
    def __init__(self, controller: Controller, settings: Settings, lid_available: bool) -> None:
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._icon_idle = QIcon(str(ICONS / "wakeful-idle.svg"))
        self._icon_watching = QIcon(str(ICONS / "wakeful-watching.svg"))
        self._icon_active = QIcon(str(ICONS / "wakeful-active.svg"))
        self._icon_active_screen = QIcon(str(ICONS / "wakeful-active-screen.svg"))

        controller.set_mode(settings.mode)
        for key, _label, _installed in controller.agents():
            controller.set_watched(key, settings.watched(key))
        controller.set_grace(settings.agent_grace)
        controller.set_agents_enabled(settings.agents_enabled)

        self.icon = QSystemTrayIcon(self._icon_idle)
        self.icon.activated.connect(self._activated)
        self._menu = QMenu()
        self._sessions_shown: tuple[AgentSession, ...] = ()
        self._build_menu(lid_available)
        self.icon.setContextMenu(self._menu)

        controller.on_changed = self.update
        controller.on_ended = self._ended
        self.update(controller.status())
        self.icon.show()

    # -- menu --------------------------------------------------------------

    def _build_menu(self, lid_available: bool) -> None:
        m = self._menu
        self._status_action = m.addAction("")
        self._status_action.setEnabled(False)
        self._sessions_menu = m.addMenu("")
        self._sessions_menu.menuAction().setVisible(False)
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
        self._start_menu.addSeparator()
        self._agents_action = self._start_menu.addAction("Agentic checks enabled")
        self._agents_action.setCheckable(True)
        self._agents_action.toggled.connect(self._set_agents_enabled)
        self._agent_actions: dict[str, QAction] = {}
        for key, label, _installed in self._controller.agents():
            action = self._start_menu.addAction(label)
            action.setCheckable(True)
            action.toggled.connect(lambda value, k=key: self._set_watched(k, value))
            self._agent_actions[key] = action
        self._grace_menu = self._start_menu.addMenu("Keep awake after agents stop")
        self._grace_group = QActionGroup(self)
        self._grace_actions: dict[float, QAction] = {}
        for seconds in GRACE_PRESETS:
            action = self._grace_menu.addAction(describe_grace(seconds).capitalize())
            action.setCheckable(True)
            action.setActionGroup(self._grace_group)
            action.triggered.connect(lambda _=False, s=seconds: self._set_grace(s))
            self._grace_actions[seconds] = action
        self._grace_custom = self._grace_menu.addAction("Custom…")
        self._grace_custom.setCheckable(True)
        self._grace_custom.setActionGroup(self._grace_group)
        self._grace_custom.triggered.connect(self._set_grace_custom)
        self._start_menu.aboutToShow.connect(lambda: self._show_agents(self._controller.status()))

        self._extend_menu = m.addMenu("Add time")
        for seconds in EXTENSIONS:
            self._extend_menu.addAction(f"+{describe_duration(seconds)}").triggered.connect(
                lambda _=False, s=seconds: self._extend(s)
            )
        self._extend_menu.addAction("Custom…").triggered.connect(self._extend_custom)
        self._stop_action = m.addAction("Stop the timer")
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
        self._mode_actions[status.mode].setChecked(True)
        if self._lid_action.isVisible() and self._lid_action.isChecked() != status.ignore_lid:
            _set_checked(self._lid_action, status.ignore_lid)
        self._show_agents(status)
        self._save_settings(status)
        self._show_sessions(status.sessions if status.held else ())

        if status.held:
            screen = status.mode is Mode.SCREEN
            self.icon.setIcon(self._icon_active_screen if screen else self._icon_active)
        elif status.watching:
            self.icon.setIcon(self._icon_watching)
        else:
            self.icon.setIcon(self._icon_idle)

        if status.active:
            lines = [f"Wakeful: {describe_remaining(status.remaining)}", status.mode.label]
        elif status.held and status.grace_left is not None:
            stopped = f"agents stopped, {describe_remaining(status.grace_left)}"
            lines = [f"Wakeful: {stopped}", status.mode.label]
        elif status.held:
            lines = ["Wakeful: agents working", status.mode.label]
        elif status.watching:
            lines = ["Wakeful: watching agents", "Sleep allowed until one is working"]
        else:
            lines = ["Wakeful: sleep allowed"]
        if status.held and status.sessions:
            lines.append(f"Agents: {session_count(len(status.sessions))}")
        if status.lid_ignored:
            lines.append("Closing the lid will not sleep")
        lines.append(self._click_hint())
        self.icon.setToolTip("\n".join(lines))

    def _show_agents(self, status: Status) -> None:
        if self._agents_action.isChecked() != status.agents_enabled:
            _set_checked(self._agents_action, status.agents_enabled)
        for key, label, installed in self._controller.agents():
            action = self._agent_actions[key]
            action.setText(label if installed else f"{label} (not installed)")
            action.setEnabled(status.agents_enabled and installed)
            if action.isChecked() != (key in status.watched):
                _set_checked(action, key in status.watched)
        self._grace_menu.setEnabled(status.agents_enabled)
        preset = self._grace_actions.get(status.grace)
        # Not _set_checked: the exclusive group unchecks the others on the
        # signal it would block. These act on triggered, not toggled.
        (preset or self._grace_custom).setChecked(True)
        custom = "Custom…" if preset else f"Custom: {describe_grace(status.grace)}…"
        self._grace_custom.setText(custom)

    def _save_settings(self, status: Status) -> None:
        """Keep the settings in step, including changes made over D-Bus. While a
        timer runs, status.mode is the timer's, which need not be the chosen one."""
        if not status.active and self._settings.mode is not status.mode:
            self._settings.mode = status.mode
        if self._settings.agent_grace != status.grace:
            self._settings.agent_grace = status.grace
        if self._settings.agents_enabled != status.agents_enabled:
            self._settings.agents_enabled = status.agents_enabled
        for key in self._agent_actions:
            if self._settings.watched(key) != (key in status.watched):
                self._settings.set_watched(key, key in status.watched)

    def _show_sessions(self, sessions: tuple[AgentSession, ...]) -> None:
        """List the working sessions, rebuilding the submenu only when they change."""
        if sessions == self._sessions_shown:
            return
        self._sessions_shown = sessions
        menu = self._sessions_menu
        menu.clear()
        menu.setTitle(f"Agents working ({len(sessions)})")
        menu.menuAction().setVisible(bool(sessions))
        for s in sessions:
            # A single & in action text marks a keyboard shortcut and is not shown.
            name = s.label.replace("&", "&&")
            menu.addAction(f"{s.agent}: {name} — {s.doing}").setEnabled(False)

    # -- the click cycle: off → agentic checks → agentic checks and a timer ---

    def _can_watch(self) -> bool:
        watched = self._controller.status().watched
        return any(key in watched and ok for key, _, ok in self._controller.agents())

    def _next_click(self) -> str:
        status = self._controller.status()
        if status.active:
            return "off"
        if status.watching or not self._can_watch():
            return "timer"
        return "agents"

    def _click_hint(self) -> str:
        step = self._next_click()
        if step == "off":
            return "Click to turn off"
        if step == "agents":
            return "Click to watch agents"
        length = describe_duration(self._settings.last_duration)
        return f"Click to keep awake for {length}"

    # -- actions -----------------------------------------------------------

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        step = self._next_click()
        if step == "off":
            # Agentic checks first: stopping the timer first would hold for
            # working agents only to release again.
            self._guard(lambda: self._controller.set_agents_enabled(False))
            self._controller.stop()
        elif step == "agents":
            self._guard(lambda: self._controller.set_agents_enabled(True))
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

    def _set_agents_enabled(self, value: bool) -> None:
        self._guard(lambda: self._controller.set_agents_enabled(value))

    def _set_grace(self, seconds: float) -> None:
        self._guard(lambda: self._controller.set_grace(seconds))

    def _set_grace_custom(self) -> None:
        text, ok = QInputDialog.getText(
            None,
            "Keep awake after agents stop",
            "How long (for example 30s, 10m, 1h, or 0 for no extra time):",
        )
        if ok and text.strip():
            try:
                self._set_grace(parse_grace(text))
                return
            except ValueError as e:
                self._error(str(e))
        self.update(self._controller.status())

    def _set_watched(self, key: str, value: bool) -> None:
        self._guard(lambda: self._controller.set_watched(key, value))

    def _set_ignore_lid(self, value: bool) -> None:
        self._settings.ignore_lid = value
        self._guard(lambda: setattr(self._controller, "ignore_lid", value))

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
        if why != "expired" or not self._settings.notify:
            return
        status = self._controller.status()
        if status.held and status.grace_left is not None:
            left = describe_remaining(status.grace_left)
            text = f"Time is up. Still keeping awake: agents stopped, {left}."
        elif status.held:
            text = "Time is up. Still keeping awake while agents are working."
        else:
            text = "Time is up. The computer can sleep again."
        self.icon.showMessage("Wakeful", text, self._icon_idle, 10000)


def _set_checked(action: QAction, value: bool) -> None:
    action.blockSignals(True)
    action.setChecked(value)
    action.blockSignals(False)
