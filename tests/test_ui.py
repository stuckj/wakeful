import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from wakeful import app as cli
from wakeful.claude import ClaudeSession
from wakeful.controller import Controller
from wakeful.tray import Settings, Tray


class Inhibitor:
    def hold(self, mode, reason):
        pass

    def release(self):
        pass


class Claude:
    def __init__(self):
        self.installed = True
        self.sessions = []

    def available(self):
        return self.installed

    def working(self):
        return self.sessions


@pytest.fixture
def tray(tmp_path):
    _ = QApplication.instance() or QApplication([])
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path))
    claude = Claude()
    controller = Controller(Inhibitor(), None, claude=claude)
    t = Tray(controller, Settings(), lid_available=False)
    yield t, controller, claude
    t.icon.hide()


def test_refused_while_claude_start_does_not_become_the_click_default(tray):
    t, controller, claude = tray
    claude.installed = False
    t._start_while_claude()
    assert not controller.status().active
    assert not Settings().last_while_claude
    assert "Click to keep awake for 1 hour" in t.icon.toolTip()

    claude.installed = True
    t._start_while_claude()
    assert controller.status().while_claude
    assert Settings().last_while_claude


def test_tooltip_says_sleep_is_allowed_while_claude_is_idle(tray):
    t, controller, claude = tray
    t._start_while_claude()
    assert "sleep allowed until Claude is working" in t.icon.toolTip()
    claude.sessions = [ClaudeSession(1, "a", "busy", "/w")]
    controller.tick()
    assert "Wakeful: while Claude is working" in t.icon.toolTip()


def test_idle_tooltip_matches_what_a_click_starts(tray):
    t, controller, claude = tray
    claude.sessions = [ClaudeSession(1, "a", "busy", "/w")]
    t._start_while_claude()
    controller.stop()
    assert "Click to keep awake while Claude is working" in t.icon.toolTip()
    claude.installed = False
    t.update(controller.status())
    assert "Click to keep awake for 1 hour" in t.icon.toolTip()


def test_session_names_are_shown_as_written(tray):
    t, _, claude = tray
    claude.sessions = [ClaudeSession(1, "R&D notes", "shell", "/w")]
    t._start_while_claude()
    [action] = t._claude_menu.actions()
    assert action.iconText() == "R&D notes — running a shell"


def test_status_from_a_tray_without_the_claude_fields(monkeypatch, capsys):
    old = {"active": True, "mode": "sleep", "remaining": 600, "lid_ignored": False}
    monkeypatch.setattr("PyQt6.QtCore.QCoreApplication", lambda *args: None)
    monkeypatch.setattr(cli.control, "call", lambda *args: {"ok": True, "status": old})
    assert cli.main(["status"]) == 0
    assert "10 min left" in capsys.readouterr().out
