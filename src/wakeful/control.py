"""The running instance's D-Bus interface, and the client the CLI uses.

Owning BUS_NAME is also what keeps wakeful to one instance per session.
Every method answers with a JSON object, {"ok": true, "status": {...}} or
{"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QObject, pyqtClassInfo, pyqtSlot
from PyQt6.QtDBus import QDBus, QDBusAbstractAdaptor, QDBusConnection, QDBusMessage

from .block import Mode
from .controller import Controller, Status

BUS_NAME = "io.github.stuckj.Wakeful"
OBJECT_PATH = "/io/github/stuckj/Wakeful"
INTERFACE = "io.github.stuckj.Wakeful"


def status_dict(status: Status) -> dict[str, Any]:
    return {
        "active": status.active,
        "mode": status.mode.value if status.mode else None,
        "remaining": None if status.remaining is None else round(status.remaining),
        "text": status.text,
        "lid_ignored": status.lid_ignored,
        "ignore_lid": status.ignore_lid,
        "watch_claude": status.watch_claude,
        "claude_sessions": [
            {"name": s.name, "status": s.status, "cwd": s.cwd, "pid": s.pid} for s in status.claude
        ],
    }


@pyqtClassInfo("D-Bus Interface", INTERFACE)
class ControlAdaptor(QDBusAbstractAdaptor):
    def __init__(self, parent: QObject, controller: Controller) -> None:
        super().__init__(parent)
        self._controller = controller

    def _answer(self, action: Any = None) -> str:
        try:
            if action is not None:
                action()
        except Exception as e:  # reported to the caller, not raised into Qt
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True, "status": status_dict(self._controller.status())})

    @pyqtSlot(int, str, result=str)
    def Start(self, seconds: int, mode: str) -> str:
        """Start a block; seconds <= 0 runs until turned off."""
        return self._answer(
            lambda: self._controller.start(seconds if seconds > 0 else None, Mode(mode))
        )

    @pyqtSlot(int, result=str)
    def Extend(self, seconds: int) -> str:
        return self._answer(lambda: self._controller.extend(seconds))

    @pyqtSlot(str, result=str)
    def SetMode(self, mode: str) -> str:
        return self._answer(lambda: self._controller.set_mode(Mode(mode)))

    @pyqtSlot(bool, result=str)
    def SetIgnoreLid(self, value: bool) -> str:
        return self._answer(lambda: setattr(self._controller, "ignore_lid", value))

    @pyqtSlot(bool, result=str)
    def SetWatchClaude(self, value: bool) -> str:
        return self._answer(lambda: setattr(self._controller, "watch_claude", value))

    @pyqtSlot(result=str)
    def Stop(self) -> str:
        return self._answer(self._controller.stop)

    @pyqtSlot(result=str)
    def Status(self) -> str:
        return self._answer()


def serve(bus: QDBusConnection, controller: Controller) -> QObject | None:
    """Export the interface and claim BUS_NAME. None if another instance owns it."""
    holder = QObject()
    holder.adaptor = ControlAdaptor(holder, controller)  # type: ignore[attr-defined]
    if not bus.registerObject(OBJECT_PATH, holder):
        return None
    if not bus.registerService(BUS_NAME):
        bus.unregisterObject(OBJECT_PATH)
        return None
    return holder


def call(method: str, *args: Any, bus: QDBusConnection | None = None) -> dict[str, Any]:
    """Call the running instance. Raises ConnectionError if there is none."""
    bus = bus if bus is not None else QDBusConnection.sessionBus()
    msg = QDBusMessage.createMethodCall(BUS_NAME, OBJECT_PATH, INTERFACE, method)
    msg.setArguments(list(args))
    reply = bus.call(msg, QDBus.CallMode.Block, 10000)
    if reply.type() != QDBusMessage.MessageType.ReplyMessage:
        raise ConnectionError(reply.errorMessage() or reply.errorName())
    return json.loads(reply.arguments()[0])
