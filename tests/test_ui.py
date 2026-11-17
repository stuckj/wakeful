import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from wakeful import app as cli
from wakeful.agents import AgentSession
from wakeful.block import Mode
from wakeful.controller import Controller
from wakeful.tray import Settings, Tray

CLICK = QSystemTrayIcon.ActivationReason.Trigger


class Inhibitor:
    def __init__(self):
        self.held = None

    def hold(self, mode, reason):
        self.held = (mode, reason)

    def release(self):
        self.held = None


class Agent:
    def __init__(self, key, label):
        self.key, self.label = key, label
        self.installed = True
        self.sessions = []

    def available(self):
        return self.installed

    def working(self):
        return tuple(self.sessions)


@pytest.fixture
def tray(tmp_path):
    _ = QApplication.instance() or QApplication([])
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path))
    agents = {"claude": Agent("claude", "Claude Code"), "codex": Agent("codex", "Codex")}
    agents["codex"].installed = False
    made = []

    def make():
        inhibitor = Inhibitor()
        controller = Controller(inhibitor, None, agents=list(agents.values()))
        t = Tray(controller, Settings(), lid_available=False)
        made.append(t)
        return t, controller, inhibitor

    yield make, agents
    for t in made:
        t.icon.hide()


def icon_is(t, which):
    return t.icon.icon().cacheKey() == getattr(t, f"_icon_{which}").cacheKey()


def test_click_cycles_off_agents_timer_and_back_off_keeping_the_ticks(tray):
    make, _ = tray
    t, controller, inhibitor = make()
    assert icon_is(t, "idle") and "Click to watch agents" in t.icon.toolTip()

    t._activated(CLICK)
    status = controller.status()
    assert status.watching and not status.active and icon_is(t, "watching")
    assert "Click to keep awake for 1 hour" in t.icon.toolTip()

    t._activated(CLICK)
    status = controller.status()
    assert status.active and status.watching and icon_is(t, "active")
    assert "Click to turn off" in t.icon.toolTip()

    t._activated(CLICK)
    status = controller.status()
    assert not status.active and not status.agents_enabled and icon_is(t, "idle")
    assert status.watched == ("claude", "codex")
    assert inhibitor.held is None


def test_click_skips_the_agentic_step_when_no_ticked_agent_is_installed(tray):
    make, _ = tray
    t, controller, _ = make()
    controller.set_watched("claude", False)
    assert "Click to keep awake for 1 hour" in t.icon.toolTip()
    t._activated(CLICK)
    assert controller.status().active and not controller.status().agents_enabled
    t._activated(CLICK)
    assert not controller.status().active


def test_icon_opens_while_an_agent_works(tray):
    make, agents = tray
    t, controller, _ = make()
    controller.set_agents_enabled(True)
    assert icon_is(t, "watching")
    agents["claude"].sessions = [AgentSession("Claude Code", 1, "a", "working", "/w")]
    controller.tick()
    assert icon_is(t, "active")
    assert "Wakeful: agents working" in t.icon.toolTip()


def test_agentic_checks_are_remembered(tray):
    make, _ = tray
    t, controller, _ = make()
    controller.set_agents_enabled(True)
    controller.set_watched("claude", False)
    t.icon.hide()
    _, again, _ = make()
    status = again.status()
    assert status.agents_enabled and "claude" not in status.watched


def test_a_mode_chosen_over_d_bus_is_remembered_and_used_by_a_click(tray):
    make, _ = tray
    t, controller, inhibitor = make()
    controller.set_watched("claude", False)
    controller.set_mode(Mode.SCREEN)
    assert Settings().mode is Mode.SCREEN
    t._activated(CLICK)
    assert controller.status().active and inhibitor.held[0] is Mode.SCREEN


def test_the_grace_menu_is_greyed_with_the_agents_and_remembers_its_choice(tray):
    make, _ = tray
    t, controller, _ = make()
    assert not t._grace_menu.isEnabled()
    assert t._grace_actions[60].isChecked()  # the default
    controller.set_agents_enabled(True)
    assert t._grace_menu.isEnabled()
    t._grace_actions[300].trigger()
    assert controller.status().grace == 300 and Settings().agent_grace == 300
    t.icon.hide()
    t2, again, _ = make()
    assert again.status().grace == 300 and t2._grace_actions[300].isChecked()


def test_a_grace_not_in_the_presets_shows_on_custom(tray):
    make, _ = tray
    t, controller, _ = make()
    controller.set_grace(45)
    assert t._grace_custom.isChecked()
    assert t._grace_custom.text() == "Custom: 45 seconds…"


def test_grace_command_needs_a_length(capsys):
    assert cli.main(["agents", "grace", "forever"]) == 2
    assert "needs a length" in capsys.readouterr().err


def test_agent_entries_are_greyed_while_agentic_checks_are_off(tray):
    make, _ = tray
    t, controller, _ = make()
    claude, codex = t._agent_actions["claude"], t._agent_actions["codex"]
    assert not claude.isEnabled()
    controller.set_agents_enabled(True)
    assert claude.isEnabled() and claude.isChecked()
    assert not codex.isEnabled() and codex.text() == "Codex (not installed)"


def test_session_names_are_shown_as_written(tray):
    make, agents = tray
    t, controller, _ = make()
    agents["claude"].sessions = [AgentSession("Claude Code", 1, "R&D notes", "working", "/w")]
    controller.set_agents_enabled(True)
    [action] = t._sessions_menu.actions()
    assert action.iconText() == "Claude Code: R&D notes — working"


def test_status_from_a_tray_without_the_agent_fields(monkeypatch, capsys):
    old = {"active": True, "mode": "sleep", "remaining": 600, "lid_ignored": False}
    monkeypatch.setattr("PyQt6.QtCore.QCoreApplication", lambda *args: None)
    monkeypatch.setattr(cli.control, "call", lambda *args: {"ok": True, "status": old})
    assert cli.main(["status"]) == 0
    assert "10 min left" in capsys.readouterr().out


def test_agents_command_needs_on_or_off(capsys):
    assert cli.main(["agents", "claude", "maybe"]) == 2
    assert "expected 'on' or 'off'" in capsys.readouterr().err
