"""Command line entry point: run the tray, or send a command to the running one."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys

from . import __version__, control
from .block import Mode, describe_remaining, parse_duration
from .claude import WORKING

log = logging.getLogger("wakeful")

TICK_MAX_MS = 5000


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wakeful",
        description="Keep a KDE Plasma session awake for a while. With no command, "
        "runs the tray icon; with one, tells the running tray what to do.",
    )
    p.add_argument("--version", action="version", version=f"wakeful {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    p.add_argument(
        "--no-tray",
        action="store_true",
        help="run without a tray icon, controlled only over D-Bus and this command",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    start = sub.add_parser("start", help="keep awake for DURATION (default: until stopped)")
    start.add_argument("duration", nargs="?", default="forever", help="e.g. 90, 45m, 1h30m")
    start.add_argument(
        "--screen", action="store_true", help="keep the screen on too (default: it may turn off)"
    )
    extend = sub.add_parser("extend", help="add DURATION to the running block")
    extend.add_argument("duration")
    sub.add_parser("stop", help="end the running block")
    sub.add_parser("status", help="show what is being kept awake")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="wakeful: %(levelname)s: %(message)s",
    )
    if args.command:
        return run_command(args)
    return run_app(no_tray=args.no_tray)


def run_command(args: argparse.Namespace) -> int:
    from PyQt6.QtCore import QCoreApplication

    _app = QCoreApplication(sys.argv[:1])
    try:
        if args.command == "start":
            seconds = parse_duration(args.duration)
            mode = Mode.SCREEN if args.screen else Mode.SLEEP
            result = control.call("Start", int(seconds or 0), mode.value)
        elif args.command == "extend":
            seconds = parse_duration(args.duration)
            if seconds is None:
                raise ValueError("extend needs a length")
            result = control.call("Extend", int(seconds))
        elif args.command == "stop":
            result = control.call("Stop")
        else:
            result = control.call("Status")
    except ValueError as e:
        print(f"wakeful: {e}", file=sys.stderr)
        return 2
    except ConnectionError as e:
        print(f"wakeful: is the tray running? {e}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print(f"wakeful: {result.get('error')}", file=sys.stderr)
        return 1
    status = result["status"]
    sessions = status.get("claude_sessions", [])
    if status["active"]:
        mode = Mode(status["mode"])
        print(f"Keeping awake ({mode.label.lower()}): {describe_remaining(status['remaining'])}")
        if status["lid_ignored"]:
            print("Closing the lid will not sleep.")
    elif sessions:
        print("Keeping awake while Claude is working.")
    else:
        print("Not keeping awake.")
    if sessions:
        print("Claude sessions working:")
        for s in sessions:
            doing = WORKING.get(s["status"], s["status"])
            print(f"  {s['name']} ({doing}) in {s['cwd']}")
    return 0


def lid_tools_available() -> bool:
    return bool(shutil.which("kreadconfig6") and shutil.which("kwriteconfig6"))


def run_app(no_tray: bool) -> int:
    from PyQt6.QtCore import QCoreApplication, QTimer
    from PyQt6.QtDBus import QDBusConnection

    from .claude import SessionWatcher, config_dir
    from .controller import Controller
    from .inhibit import DBusInhibitor
    from .lid import KConfigTools, LidError, LidGuard
    from .lidwatch import WatchedLidGuard
    from .powerdevil import refresh_status

    if no_tray:
        app = QCoreApplication(sys.argv[:1])
    else:
        from PyQt6.QtWidgets import QApplication

        app = QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        app.setDesktopFileName("wakeful")
    app.setApplicationName("wakeful")
    app.setApplicationVersion(__version__)

    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        log.error("no session D-Bus: %s", bus.lastError().message())
        return 1

    guard = LidGuard(KConfigTools(), refresh_status) if lid_tools_available() else None
    if guard is None:
        log.info("kreadconfig6/kwriteconfig6 not found; the lid option is unavailable")
    lid = WatchedLidGuard(guard) if guard is not None else None

    claude = SessionWatcher() if config_dir().is_dir() else None
    if claude is None:
        log.info("%s not found; the Claude Code option is unavailable", config_dir())

    controller = Controller(DBusInhibitor(bus), lid, claude=claude)
    holder = control.serve(bus, controller)
    if holder is None:
        print("wakeful is already running.", file=sys.stderr)
        return 0

    # Only once the bus name is ours: a journal while another instance runs is
    # that instance's live block, not something left behind.
    if guard is not None and guard.engaged:
        log.warning("restoring the lid action left changed by an earlier run")
        try:
            guard.restore()
        except LidError:
            log.exception("could not restore the lid action; leaving %s", guard.journal)

    if no_tray:
        tray = None
    else:
        from .tray import Settings, Tray

        settings = Settings()
        if lid is not None:
            controller.ignore_lid = settings.ignore_lid
        if claude is not None:
            controller.watch_claude = settings.watch_claude
        tray = Tray(
            controller,
            settings,
            lid_available=lid is not None,
            claude_available=claude is not None,
        )

    timer = QTimer()
    timer.setSingleShot(True)

    def tick() -> None:
        controller.tick()
        remaining = controller.status().remaining
        ms = TICK_MAX_MS if remaining is None else int(min(TICK_MAX_MS, remaining * 1000 + 50))
        timer.start(max(ms, 100))

    timer.timeout.connect(tick)
    tick()

    # Qt's loop does not return to Python on its own, so a signal would wait
    # for the next event; this idle timer lets the handler run promptly.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: app.quit())
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)

    app.aboutToQuit.connect(controller.shutdown)
    code = app.exec()
    del tray, holder
    return code
